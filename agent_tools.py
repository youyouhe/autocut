# agent_tools.py — 聊天 Agent 工具层 (自 render_server.py 端点闭包抽出)
# 28 个工具的 schema (TOOL_SCHEMAS) + 单一分发点 execute_tool(name, args, ctx).
# 所有防御行为 (冷草稿 warmup / create_draft 复用铁律 / render_status 阻塞等待 /
# 未知工具显式报错 / 动画名校验 / 自动接龙) 原样保留.
#
# OpenAI Agents SDK 适配: build_tools(ctx) 把每个 schema 包装成 agents.FunctionTool,
# on_invoke_tool 统一走 execute_tool —— schema 与实现同源, 无二次转写.
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from agents import FunctionTool, RunContextWrapper


@dataclass
class ToolContext:
    """单次请求的工具执行上下文 (对应原端点闭包捕获的自由变量)."""
    uid: str                                    # 多租户用户 id
    asset_paths: List[str]                      # 当前租户素材绝对路径
    draft_id: str = ''                          # 激活草稿 (可变; 工具会写回)
    on_draft_created: Callable[[str], None] = lambda d: None  # 新草稿通知 (端点发 draft_id 帧)
    on_tool_executed: Callable[[str, dict, str], None] = lambda n, a, r: None  # 工具回执 (端点发 tool 帧)


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_resources",
            "description": "列出所有已上传的资源（名称、类型、是否已分析、已有标签）。用户问'有哪些素材'时调用。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_main_video",
            "description": "获取当前被标记为'主视频'的素材(每次最新录制/最新一期的那条，跟长期存在的素材库分开管理，由用户在界面手动标记)。用户说'主视频'/'这次的视频'/'最新录的'而没指定具体文件名时，先调用这个解析出实际文件名，不要直接去猜或要求用户重复报文件名。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_by_tags",
            "description": "按关键词标签快速检索素材 —— SQLite 索引查询, 不调用 LLM/网络, 毫秒级返回。素材数量多时,先用这个粗筛出候选文件名, 缩小范围再对具体某个文件调用 get_resource_detail/get_transcript 看全文细节, 避免一次性把所有素材的完整描述都塞进对话上下文浪费 token。用户想找'带山的视频''风景图'这类按内容筛选素材的需求时优先用这个。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "要匹配的关键词, 如 ['山','水塘','风景']"},
                    "type": {"type": "string", "description": "只筛选某类素材: video/image/audio, 可选"}
                },
                "required": ["keywords"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_assets",
            "description": "按自由文本全文检索素材 —— SQLite 模糊查询, 在文件名/画面描述(VLM)/口播文案(ASR)/标签里搜, 不调用 LLM/网络。当标签检索(search_by_tags)不够精确、或用户用整句话描述要找的内容(如'有晚霞和凉亭的镜头')时, 用这个全文检索兜底。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索文本, 如 '晚霞 凉亭' 或 '河边风景'"},
                    "type": {"type": "string", "description": "只筛选某类素材: video/image/audio, 可选"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_resource_detail",
            "description": "查询单个资源的完整分析：画面描述(VLM)、口播文案(ASR)、时间轴、元数据。用户问'视频讲了什么/文案是什么/内容是什么'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "资源文件名（如 file (3).mp4）"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_transcript",
            "description": "获取视频的语音转录文案（含时间戳）。用户问'口播文案/字幕/语音内容'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "资源文件名"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_resource",
            "description": "对一个尚未分析的视频/图片资源做内容分析并缓存结果 (视频: 画面VLM+语音ASR; 图片: 直接VLM看图)。用户要求分析/查看内容而资源显示'未分析'时，直接调用这个工具自己触发分析，不要让用户去点界面按钮。视频较长时可能耗时数十秒。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "资源文件名"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "split_shots",
            "description": "对一个视频做分镜拆分：检测镜头边界(切镜点), 并把每个镜头切成独立的小视频文件+关键帧。用户要求'分镜/拆镜头/按镜头切开'时调用。已经拆过的直接返回缓存结果, 不重复拆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "视频资源文件名"},
                    "force": {"type": "boolean", "description": "忽略缓存重新拆分, 默认 false"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_draft",
            "description": "获取一个可用草稿用于编辑。已存在激活草稿时直接复用当前草稿（不会新建空草稿、不丢失已有内容），只有当前无草稿时才真正新建。绝大多数情况不需要传参。只有用户明确说“新建草稿/重新做一个/另起一个”才需要 force_new=true 强制新建。返回 draft_id。",
            "parameters": {
                "type": "object",
                "properties": {
                    "force_new": {"type": "boolean", "description": "强制新建一个空草稿（默认 false）。仅当用户明确要求“新建/重新做”时设 true。已有草稿时若误设 true 会丢失当前草稿上下文。", "default": False}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "use_draft",
            "description": "确认/切换当前激活草稿。要继续在已有草稿上编辑（加段/补字幕/渲染）时调用：不传 draft_id = 确认沿用当前激活草稿；传 draft_id = 切换到指定的已有草稿（从 list_drafts / get_draft_timeline 拿到的 id）。用它来“接着做”，不要用 create_draft（create_draft 在已有草稿时只是复用，语义混淆）。返回当前草稿的时间线摘要。",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string", "description": "要切换到的草稿 id。不传则沿用当前激活草稿。"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_video",
            "description": "添加视频到草稿。默认接在主视频轨道('video_main')末尾按顺序拼接。要把某段视频作为'补充素材/花絮/B-roll'叠加显示在主视频的某个时间点上方时，必须指定不同的 track_name 和该素材应出现的 target_start，否则会和主视频挤在同一条轨道上互相覆盖。默认操作当前激活草稿；如需操作其他草稿可传 draft_id 指定。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "视频URL或路径"},
                    "start": {"type": "number", "description": "源视频截取起始秒(素材文件内的秒数)", "default": 0},
                    "end": {"type": "number", "description": "源视频截取结束秒(素材文件内的秒数)"},
                    "target_start": {"type": "number", "description": "这段素材在成片时间轴上应该出现的秒数(不是素材源文件的秒数)。不填=自动接在同名轨道已有内容末尾"},
                    "track_name": {"type": "string", "description": "轨道名，默认 'video_main'(主视频轨道)。叠加补充素材时用不同的名字，如 'broll_1'"},
                    "relative_index": {"type": "integer", "description": "轨道层级，数值越大越靠上层显示。叠加在主视频上方要设成比主视频轨道更高的值(如 1)，否则会被主视频盖住而不是盖住主视频"},
                    "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_subtitle",
            "description": "按 SRT 内容批量加字幕轨(一条 cue 一段, 时间轴来自 SRT 本身, 和语音天然同步). "
                           "做视频字幕【必须】用这个工具, 禁止用 add_text 一条条手动排字幕 (拿不到真实语音时间点, "
                           "排出来必然不同步). SRT 从 get_transcript 拿 (返回里有 srt 字段) 或用户直接提供.",
            "parameters": {
                "type": "object",
                "properties": {
                    "srt": {"type": "string", "description": "SRT 全文 (标准格式: 序号/起止时间行/文本行), 不是文件路径"},
                    "time_offset": {"type": "number", "description": "整体时间偏移秒数, 默认0"},
                    "font_size": {"type": "number", "description": "字号, 默认5"},
                    "font_color": {"type": "string", "description": "字体颜色十六进制, 默认 '#FFFFFF'"},
                    "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                },
                "required": ["srt"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_text",
            "description": "添加文字到草稿；不止字幕，也可用于标题/水印/角标等任意文字标识——通过 track_name 区分轨道、transform_x/transform_y 控制画面位置。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "文字内容"},
                    "start": {"type": "number", "description": "开始秒"},
                    "end": {"type": "number", "description": "结束秒"},
                    "track_name": {"type": "string", "description": "轨道名，默认 'text_main'（字幕轨）；做独立文字标识/标题/水印时用不同轨道名（如 'label_1'）避免和字幕冲突叠压"},
                    "transform_x": {"type": "number", "description": "水平位置，-1(左)~1(右)，0为居中，默认0"},
                    "transform_y": {"type": "number", "description": "垂直位置，-1(底)~1(顶)，默认-0.8（画面下方，字幕常用位置）；标题/角标常用 0.7~0.9（画面上方）"},
                    "font_size": {"type": "number", "description": "字号，默认12"},
                    "font_color": {"type": "string", "description": "字体颜色，十六进制，默认 '#FFFFFF'"},
                    "background_color": {"type": "string", "description": "文字背景色（如水印底色），默认不显示背景"},
                    "background_alpha": {"type": "number", "description": "背景不透明度 0.0~1.0，默认0（无背景，纯色块不是毛玻璃/模糊效果——本工具不支持真实的背景模糊/毛玻璃特效）"},
                    "intro_animation": {"type": "string", "description": "入场动画名，如 'Random_Typewriter'(打字机)/'Blur_to_the_Left'(左移模糊)/'Bounce_from_TR'(右上弹入) 等，需精确匹配预置动画名，不确定就别填"},
                    "outro_animation": {"type": "string", "description": "出场动画名，同 intro_animation 命名规则，如 'Blur_to_the_Left'/'Horizontal_Close' 等，不确定就别填"},
                    "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                },
                "required": ["text", "start", "end"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_text_animations",
            "description": "查询 add_text 可用的入场/出场动画名字列表，调用 add_text 的 intro_animation/outro_animation 前先查一下，别瞎猜名字。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["intro", "outro"], "description": "查入场动画还是出场动画"}
                },
                "required": ["kind"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_audio",
            "description": "给草稿添加背景音乐/音频。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "音频URL或本地路径"},
                    "start": {"type": "number", "description": "插入到草稿时间轴的开始秒", "default": 0},
                    "end": {"type": "number", "description": "结束秒（不填=到音频末尾）"},
                    "volume": {"type": "number", "description": "音量 0-1", "default": 0.5}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_image",
            "description": "给草稿添加图片（作为一段画面）。默认接在图片轨道('image_main')末尾顺序展示。要把图片作为'补充素材'叠加显示在主视频的某个时间点上方时，指定 start/end 为该时间点在成片时间轴上的秒数（而不是省略靠自动接龙），必要时指定更高的 relative_index 确保盖住主视频画面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "图片URL或本地路径"},
                    "start": {"type": "number", "description": "插入到草稿时间轴的开始秒。不填=自动接在图片轨道已有内容末尾"},
                    "end": {"type": "number", "description": "结束秒。不填=从 start 起默认展示 3 秒"},
                    "track_name": {"type": "string", "description": "轨道名，默认 'image_main'。跟主视频叠加时可以保持默认(图片轨默认就在视频轨上方)"},
                    "relative_index": {"type": "integer", "description": "轨道层级，数值越大越靠上层显示"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_segment",
            "description": "从草稿删除一个轨道上的单个片段(段)。用户说'删掉某段/去掉后两段/这段不要了'时用。删除后底层会自动清理孤儿素材引用并重算总时长, 不用手动处理。定位方式二选一: (1) track_name + index —— 指定轨道第几个片段(从0开始); (2) segment_id —— 精确匹配(从 get_draft_timeline 拿不到 segment_id, 一般用 index 定位即可)。强烈建议: 先 get_draft_timeline 看清各段在哪个轨道、index 是几, 再删, 别凭记忆猜; 删一段后该轨道后面的段 index 会前移, 连删多段时从后往前删(index 不会乱)。删完可再 get_draft_timeline 复核。",
            "parameters": {
                "type": "object",
                "properties": {
                    "track_name": {"type": "string", "description": "轨道名, 如 'video_main'/'broll_1'/'text_main'。与 index 配合定位片段。"},
                    "index": {"type": "integer", "description": "该轨道上要删的片段序号, 从0开始(第1段=0)。与 track_name 配合。连删多段时从最大的 index 往前删。"},
                    "segment_id": {"type": "string", "description": "片段唯一 id, 精确匹配(可选, 一般不需要, 用 track_name+index 即可)。"},
                    "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                },
                "required": ["track_name", "index"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_track",
            "description": "删除一整条轨道(含其上所有片段)。用户说'把整个花絮轨/补充素材轨都删掉/不要这条轨'时用, 比逐段 delete_segment 更快。注意: 主轨道(video_main)删了草稿基本就空了, 删主轨道前跟用户确认。删完底层自动清理孤儿素材+重算时长。定位方式(三选一, 优先用前两种避免同名歧义): (1) track_id —— 从 get_draft_timeline 返回的 track_id 字段拿, 同名轨道也能精确删指定那一条(推荐); (2) delete_all=true + track_name —— 一次删掉所有同名的轨道; (3) track_name —— 删第一个匹配项, 若同名轨道>1条会返回 ambiguous:true, 此时应改用 track_id 或 delete_all。先 get_draft_timeline 看清再删。",
            "parameters": {
                "type": "object",
                "properties": {
                    "track_name": {"type": "string", "description": "要删除的轨道名, 如 'broll_1'/'broll_2'。与 track_id 二选一。"},
                    "track_id": {"type": "string", "description": "轨道唯一 id(从 get_draft_timeline 的 track_id 字段拿), 同名轨道消歧用, 精确删指定一条。"},
                    "delete_all": {"type": "boolean", "description": "为 true 时删除所有同名 track_name 的轨道。默认 false。"},
                    "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_empty_tracks",
            "description": "删除所有零片段的空轨道。用户说'删掉空轨道/清掉空的 video 轨/有空轨道不要了'时用——一次清掉所有空轨道, 比逐条 delete_track 更省事, 也绕开同名歧义。可选用 track_type(如 'video')或 track_name 进一步过滤。建议先 get_draft_timeline 确认哪些是空的(is_empty:true)。删完底层自动清理孤儿素材+重算时长, 返回删除的轨道列表(含 track_id)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "track_type": {"type": "string", "description": "只删该类型的空轨道, 如 'video'/'audio'/'text'（可选）。"},
                    "track_name": {"type": "string", "description": "只删该名字的空轨道, 如 'video'（可选, 用于精准清理某种预建垃圾轨）。"},
                    "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_draft",
            "description": "保存草稿到磁盘。默认保存当前激活草稿；如需保存其他草稿可传 draft_id 指定。",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "render",
            "description": "提交渲染（自动保存草稿并渲染为mp4）。用户说'渲染/导出/出片'时调用。调用前必须已 create_draft + add_video。默认渲染当前激活草稿；如需渲染其他草稿可传 draft_id 指定。",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "render_status",
            "description": "查询一个渲染任务的进度/是否完成。render 工具返回的 task_id 传进来查。"
                           "wait=true 时服务端会阻塞等待(约25秒/次)直到状态变化或完成再返回 —— 自动监控时用它, "
                           "免得连续空查; 反复调用直到 status 变为 done/error 即完成监控。",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string", "description": "render 工具返回的任务ID"},
                               "wait": {"type": "boolean", "description": "服务端等待到状态变化再返回(默认false立即返回)"}},
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bsk_run",
            "description": "执行一条 BrowserSkill(bsk) CLI 命令, 驱动用户已登录的浏览器完成网页操作"
                           "(如把渲染好的视频发布到视频号/抖音/小红书). 命令原样传给 bsk, 返回 stdout/stderr. "
                           "标准生命周期: bsk session start 拿会话id → 各命令都带 --session <id> → 最后必须 bsk session stop <id>.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "bsk 子命令(不含 bsk 前缀), 如 'session start' / 'navigate https://... --session ab12' / 'snapshot --session ab12'"},
                    "timeout": {"type": "number", "description": "秒, 默认 60; request-help 等待用户时给 300"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_drafts",
            "description": "列出磁盘上已保存的草稿（名称/时长/修改时间）。用户问'有哪些草稿/之前做的视频'时调用。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_draft_timeline",
            "description": "读取草稿的时间线:每条轨道上的素材(文件名/类型)、起止秒数、轨道名、总时长。用于了解草稿现状再编辑——用户说'草稿里有什么/现在组装成什么样了/在第二段后面加/把某段换掉/还差什么'时先调用这个看现状。默认读当前激活草稿；传 draft_id 可读指定草稿。无激活草稿时返回提示。注意:每段返回 name(草稿内部名, 形如 video_xxx.mp4, 不可直接用于查内容) 和 source_name(原始文件名, 如 VID_xxx.mp4)。要查某段视频讲了什么/内容时, 用 source_name 调 get_resource_detail / get_transcript, 不要用 name。每条轨道还返回 track_id(稳定唯一id) 和 is_empty(是否零片段): 空轨道(is_empty:true, segment_count:0)也可见——用户说'有空轨道/删掉空轨'时直接据此定位; 同名轨道重复时用 track_id 精确指定删除哪一条(传给 delete_track 的 track_id)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string", "description": "目标草稿 id（可选）。不传=用当前激活草稿。"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_templates",
            "description": "列出可用的视频模板及每个模板需要填的变量名。用户想用模板快速做视频时先调用这个看有什么模板。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_template",
            "description": "用预设模板一次性生成草稿（自动创建草稿+按模板组装所有场景+保存，返回 draft_id）。适合用户说'用XX模板做个视频'时调用。先用 list_templates 确认模板名和需要的变量。",
            "parameters": {
                "type": "object",
                "properties": {
                    "template": {"type": "string", "description": "模板文件名（不含.yaml），如 product_intro"},
                    "variables": {"type": "object", "description": "模板变量填充，key 为变量名（如 product_name/demo_video）"}
                },
                "required": ["template", "variables"]
            }
        }
    },
]

def _find_analysis(path, uid):
    """按 path 查分析缓存. 容忍正/反斜杠差异. 多租户: 只命中本租户或 legacy NULL."""
    import memory_store as ms
    a = ms.get_analysis(path, owner=uid)
    if a:
        return a
    alt = path.replace('/', '\\') if '/' in path else path.replace('\\', '/')
    return ms.get_analysis(alt, owner=uid)

def execute_tool(name, args, ctx):
    """执行工具调用, 返回结果 JSON 字符串. ctx: ToolContext (ctx.uid/ctx.asset_paths/draft_id/回调).
    自 render_server 端点闭包抽出, 逻辑与防御行为保持不变."""
    import re as _re
    import render_server as rs  # 惰性 import 避免循环 (execute_tool 运行时 rs 已加载完)
    import memory_store as ms
    draft_id = ctx.draft_id  # 本地镜像; 三处赋值点写回 ctx
    result = {}

    if name == 'list_resources':
        import main_video_store
        main_path = (main_video_store.get(ctx.uid) or {}).get('path')
        items = []
        for path in ctx.asset_paths:
            fname = os.path.basename(path)
            ext = os.path.splitext(fname)[1].lower()
            ftype = 'video' if ext in ('.mp4','.mov','.avi','.mkv') else \
                    'image' if ext in ('.jpg','.png','.jpeg') else \
                    'audio' if ext in ('.mp3','.wav','.aac') else 'other'
            analysis = _find_analysis(path)
            analyzed = '已分析' if analysis else '未分析'
            entry = f'{fname} ({ftype}, {analyzed}'
            tags = analysis.get('tags') if analysis else None
            if tags:
                entry += ', tags: ' + '/'.join(tags)
            if path == main_path:
                entry += ', 主视频'
            entry += ')'
            items.append(entry)
        result = {'resources': items}

    elif name == 'get_main_video':
        import main_video_store
        info = main_video_store.get(ctx.uid)
        if not info:
            result = {'error': '还没有标记任何主视频，请让用户在素材面板里点"设为主视频"'}
        else:
            result = {'name': info['name'], 'path': info['path']}

    elif name == 'search_by_tags':
        keywords = [str(k).strip() for k in (args.get('keywords') or []) if str(k).strip()]
        asset_set = set(ctx.asset_paths)
        from asset_store import search_tags as _search_tags
        matches = []
        for m in _search_tags(keywords, type=args.get('type'), owner=ctx.uid):
            if m['path'] not in asset_set:
                continue
            hit = [k for k in keywords if any(k in t for t in m['tags'])]
            matches.append({'name': m['name'], 'tags': m['tags'], 'matched_keywords': hit})
        result = {'matches': matches, 'total_analyzed_scanned': sum(1 for p in ctx.asset_paths if _find_analysis(p))}

    elif name == 'search_assets':
        query = str(args.get('query') or '').strip()
        asset_set = set(ctx.asset_paths)
        from asset_store import search_text as _search_text
        matches = []
        for m in _search_text(query, type=args.get('type'), owner=ctx.uid):
            if m['path'] not in asset_set:
                continue
            matches.append({
                'name': m['name'], 'type': m['type'], 'tags': m['tags'],
                'duration': m['duration'], 'visual_snippet': m['visual'],
                'transcript_snippet': m['audio_text'],
            })
        result = {'matches': matches}

    elif name == 'get_resource_detail':
        fname = args.get('name', '')
        path = next((p for p in ctx.asset_paths if fname in p), None)
        if not path:
            return json.dumps({'error': f'未找到资源: {fname}'}, ensure_ascii=False)
        analysis = _find_analysis(path)
        if not analysis:
            return json.dumps({'error': f'{fname} 尚未分析，请调用 analyze_resource 先分析'}, ensure_ascii=False)
        detail = {'filename': fname, 'path': path}
        meta = analysis.get('meta', {})
        detail['duration'] = meta.get('duration', 0)
        detail['resolution'] = f'{meta.get("width","?")}x{meta.get("height","?")}'
        detail['tags'] = analysis.get('tags', [])
        # VLM
        visual = analysis.get('visual_analysis', '')
        try:
            m = _re.search(r'\{[\s\S]*\}', visual or '')
            if m: detail['visual'] = json.loads(m.group())
        except: detail['visual_raw'] = (visual or '')[:300]
        # ASR
        audio = analysis.get('audio', {})
        if isinstance(audio, dict):
            detail['transcript'] = audio.get('full_text', '')
            detail['segments'] = audio.get('segments', [])
        result = detail

    elif name == 'get_transcript':
        fname = args.get('name', '')
        path = next((p for p in ctx.asset_paths if fname in p), None)
        if not path:
            return json.dumps({'error': f'未找到: {fname}'}, ensure_ascii=False)
        analysis = _find_analysis(path)
        if not analysis:
            return json.dumps({'error': f'{fname} 尚未分析，请调用 analyze_resource 先分析'}, ensure_ascii=False)
        audio = analysis.get('audio', {})
        if isinstance(audio, dict):
            result = {
                'full_text': audio.get('full_text', '(无语音)'),
                'segments': audio.get('segments', []),
                # srt 全文: 喂给 add_subtitle 用 (时间轴来自语音识别, 天然同步)
                'srt': analysis.get('srt', '')
            }
        else:
            result = {'full_text': '(无语音)', 'segments': [], 'srt': ''}

    elif name == 'analyze_resource':
        fname = args.get('name', '')
        path = next((p for p in ctx.asset_paths if fname in p), None)
        if not path:
            return json.dumps({'error': f'未找到资源: {fname}'}, ensure_ascii=False)
        try:
            ext = os.path.splitext(path)[1].lower()
            if rs._ASSET_TYPE_BY_EXT.get(ext) == 'image':
                from perceive import perceive_image
                analysis = perceive_image(path)
            else:
                from perceive import perceive_video
                analysis = perceive_video(path, do_asr=True, frame_count=4)
            ms.save_analysis(path, analysis, owner=ctx.uid)
            result = {
                'ok': True,
                'analysis_mode': analysis.get('analysis_mode'),
                'duration': analysis.get('meta', {}).get('duration', 0),
            }
        except Exception as e:
            result = {'error': str(e)}

    elif name == 'split_shots':
        fname = args.get('name', '')
        path = next((p for p in ctx.asset_paths if fname in p), None)
        if not path:
            return json.dumps({'error': f'未找到资源: {fname}'}, ensure_ascii=False)
        try:
            import shot_split
            shots = shot_split.split_shots(path, force=bool(args.get('force', False)))
            result = {
                'ok': True,
                'shot_count': len(shots),
                'shots': [{'index': s['index'], 'start': s['start'], 'end': s['end'], 'duration': s['duration']}
                          for s in shots],
            }
        except Exception as e:
            result = {'error': str(e)}

    elif name == 'create_draft':
        # 草稿复用铁律: 已有激活草稿且非强制新建时, 直接复用, 不新建空草稿 ——
        # 避免 agent 在已有草稿上继续编辑时误调 create_draft 把会话草稿覆盖成空草稿。
        if draft_id and not args.get('force_new'):
            result = {'draft_id': draft_id, 'ok': True, 'note': '复用已有激活草稿（未新建）。要继续编辑直接 add_video/add_subtitle；用户明确要求"新建/重新做"再传 force_new=true。'}
        else:
            r = rs._post_internal('create_draft', {'width': 1080, 'height': 1920}, user_id=ctx.uid)
            if r.get('success') and r.get('output', {}).get('draft_id'):
                draft_id = r['output']['draft_id']
                ctx.draft_id = draft_id
                ctx.on_draft_created(draft_id)
                result = {'draft_id': draft_id, 'ok': True, 'note': '已新建空草稿。'}
            else:
                result = {'error': str(r)}

    elif name == 'use_draft':
        # 切换/确认当前激活草稿。传 draft_id → 切换到该草稿; 不传 → 沿用当前草稿。
        target = args.get('draft_id') or draft_id
        if not target:
            result = {'error': '当前无激活草稿，也没有传入 draft_id。请先 create_draft 新建，或从 list_drafts 选一个传进来。'}
        else:
            # 验证该草稿真实存在 (缓存或磁盘), 切换闭包 draft_id
            r = rs._get_internal(f'api/draft/timeline/{target}', user_id=ctx.uid)
            if not isinstance(r, dict) or not r.get('success'):
                result = {'error': f"草稿 '{target}' 不存在或读取失败: {(r.get('error') if isinstance(r, dict) else None)}。可用 list_drafts 查看草稿列表。"}
            else:
                draft_id = target
                ctx.draft_id = draft_id
                # 冷草稿热身: use_draft 只校验了磁盘存在, 但编辑工具 (delete/add/save) 要求草稿
                # 在 DRAFT_CACHE 里。服务重启后草稿会变冷, 这里就地载入, 让后续编辑能直接命中。
                warmed = rs._warmup_draft(draft_id)
                try:
                    content = json.loads(r['output'])
                    result = {
                        'draft_id': draft_id, 'ok': True,
                        'duration_s': round((content.get('duration') or 0) / 1_000_000, 3),
                        'track_count': len([t for t in (content.get('tracks') or []) if t.get('segments')]),
                        'note': '已切换/确认当前激活草稿。后续 add_video/add_subtitle/delete_segment/save_draft/render 默认操作它。',
                    }
                    if warmed:
                        result['note'] += ' (冷草稿已从磁盘载入内存, 可编辑)'
                except Exception as e:
                    # 切换成功但摘要解析失败: 仍算切换成功, 只是没有摘要
                    result = {'draft_id': draft_id, 'ok': True, 'note': f'已切换到草稿（摘要解析失败: {e}）'}

    elif name == 'add_video':
        did = args.get('draft_id') or draft_id
        if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
        track_name = args.get('track_name') or 'video_main'
        target_start = args.get('target_start')
        if target_start is None:
            target_start = rs._track_end_seconds(did, track_name)
        d = {'draft_id': did, 'video_url': rs._resolve_asset_url(args.get('url','')),
             'track_name': track_name, 'target_start': target_start}
        if args.get('relative_index') is not None: d['relative_index'] = args['relative_index']
        if args.get('start') is not None: d['start'] = args['start']
        if args.get('end') is not None: d['end'] = args['end']
        r = rs._post_internal('add_video', d, user_id=ctx.uid)
        result = {'ok': r.get('success', False), 'draft_id': did, 'track_name': track_name, 'target_start': target_start}

    elif name == 'add_subtitle':
        did = args.get('draft_id') or draft_id
        if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
        srt = args.get('srt') or ''
        if not srt.strip():
            result = {'error': 'srt 内容为空'}
        else:
            d = {'draft_id': did, 'srt': srt}
            if args.get('time_offset') is not None: d['time_offset'] = args['time_offset']
            if args.get('font_size') is not None: d['font_size'] = args['font_size']
            if args.get('font_color') is not None: d['font_color'] = args['font_color']
            r = rs._post_internal('add_subtitle', d, user_id=ctx.uid)
            result = {'ok': r.get('success', False), 'draft_id': did, 'error': r.get('error')}

    elif name == 'add_text':
        did = args.get('draft_id') or draft_id
        if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
        track_name = args.get('track_name') or 'text_main'
        d = {
            'draft_id': did, 'text': args.get('text',''),
            'start': args.get('start',0), 'end': args.get('end',5),
            'track_name': track_name,
            'font_size': args.get('font_size', 12),
            'font_color': args.get('font_color', '#FFFFFF'),
            'transform_x': args.get('transform_x', 0),
            'transform_y': args.get('transform_y', -0.8),
        }
        if args.get('background_color') is not None: d['background_color'] = args['background_color']
        if args.get('background_alpha') is not None: d['background_alpha'] = args['background_alpha']
        if args.get('intro_animation'):
            valid = rs._text_animation_names('intro')
            if valid and args['intro_animation'] not in valid:
                return json.dumps({'error': f"intro_animation 名字不存在: {args['intro_animation']!r}，先调用 list_text_animations(kind='intro') 看合法名字"}, ensure_ascii=False)
            d['intro_animation'] = args['intro_animation']
        if args.get('outro_animation'):
            valid = rs._text_animation_names('outro')
            if valid and args['outro_animation'] not in valid:
                return json.dumps({'error': f"outro_animation 名字不存在: {args['outro_animation']!r}，先调用 list_text_animations(kind='outro') 看合法名字"}, ensure_ascii=False)
            d['outro_animation'] = args['outro_animation']
        r = rs._post_internal('add_text', d, user_id=ctx.uid)
        result = {'ok': r.get('success', False), 'draft_id': did, 'track_name': track_name}

    elif name == 'list_text_animations':
        names = rs._text_animation_names(args.get('kind', 'intro'))
        result = {'kind': args.get('kind'), 'names': names} if names else {'error': '动画名字表加载失败'}

    elif name == 'add_audio':
        did = args.get('draft_id') or draft_id
        if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
        d = {'draft_id': did, 'audio_url': rs._resolve_asset_url(args.get('url','')),
             'volume': args.get('volume', 0.5)}
        if args.get('start') is not None: d['start'] = args['start']
        if args.get('end') is not None: d['end'] = args['end']
        r = rs._post_internal('add_audio', d, user_id=ctx.uid)
        result = {'ok': r.get('success', False), 'draft_id': did}

    elif name == 'add_image':
        did = args.get('draft_id') or draft_id
        if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
        track_name = args.get('track_name') or 'image_main'
        start = args.get('start')
        if start is None:
            start = rs._track_end_seconds(did, track_name)
        end = args.get('end')
        if end is None:
            end = start + 3   # 没给时长默认展示 3 秒
        d = {'draft_id': did, 'image_url': rs._resolve_asset_url(args.get('url','')),
             'track_name': track_name, 'start': start, 'end': end}
        if args.get('relative_index') is not None: d['relative_index'] = args['relative_index']
        r = rs._post_internal('add_image', d, user_id=ctx.uid)
        result = {'ok': r.get('success', False), 'draft_id': did, 'track_name': track_name, 'start': start, 'end': end}

    elif name == 'delete_segment':
        did = args.get('draft_id') or draft_id
        if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
        track_name = args.get('track_name')
        index = args.get('index')
        segment_id = args.get('segment_id')
        if segment_id is None and (not track_name or index is None):
            result = {'error': '需要提供 segment_id, 或同时提供 track_name 与 index 来定位片段。'}
        else:
            # 冷草稿防御: delete_segment 走 capcut_server → delete_impl._get_script, 要求草稿在
            # DRAFT_CACHE。服务重启后草稿会变冷 (use_draft 会热身, 但 agent 可能跳过它直接删),
            # 这里先确保载入, 否则底层抛 KeyError"不存在于缓存中"让 agent 误判草稿坏了。
            rs._warmup_draft(did)
            d = {'draft_id': did}
            if track_name is not None: d['track_name'] = track_name
            if index is not None: d['index'] = index
            if segment_id is not None: d['segment_id'] = segment_id
            r = rs._post_internal('delete_segment', d, user_id=ctx.uid)
            if r.get('success'):
                out = r.get('output', {})
                result = {
                    'ok': True, 'draft_id': did,
                    'deleted': out if isinstance(out, dict) else {'info': str(out)},
                    'duration_s': out.get('duration_sec') if isinstance(out, dict) else None,
                }
            else:
                result = {'ok': False, 'draft_id': did, 'error': r.get('error') or str(r)}

    elif name == 'delete_track':
        did = args.get('draft_id') or draft_id
        if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
        track_name = args.get('track_name')
        track_id = args.get('track_id')
        delete_all = bool(args.get('delete_all'))
        if not track_name and not track_id:
            result = {'error': 'track_name 或 track_id 至少提供一个'}
        else:
            rs._warmup_draft(did)  # 同 delete_segment: 防冷草稿 KeyError
            d = {'draft_id': did}
            if track_name is not None: d['track_name'] = track_name
            if track_id is not None: d['track_id'] = track_id
            if delete_all: d['delete_all'] = True
            r = rs._post_internal('delete_track', d, user_id=ctx.uid)
            if r.get('success'):
                out = r.get('output', {}) if isinstance(r.get('output'), dict) else {}
                # 批量删返回 deleted_tracks(列表), 单删返回 deleted_track; 都透传
                result = {'ok': True, 'draft_id': did, 'duration_s': out.get('duration_sec')}
                if 'deleted_tracks' in out:
                    result['deleted_tracks'] = out['deleted_tracks']
                    result['deleted_count'] = out.get('deleted_count')
                if 'deleted_track' in out:
                    result['deleted_track'] = out['deleted_track']
                if out.get('ambiguous'):
                    result['ambiguous'] = True
            else:
                result = {'ok': False, 'draft_id': did, 'error': r.get('error') or str(r)}

    elif name == 'delete_empty_tracks':
        did = args.get('draft_id') or draft_id
        if not did: return json.dumps({'error': '请先创建草稿'}, ensure_ascii=False)
        rs._warmup_draft(did)  # 防冷草稿 KeyError
        d = {'draft_id': did}
        if args.get('track_type'): d['track_type'] = args['track_type']
        if args.get('track_name'): d['track_name'] = args['track_name']
        r = rs._post_internal('delete_empty_tracks', d, user_id=ctx.uid)
        if r.get('success'):
            out = r.get('output', {}) if isinstance(r.get('output'), dict) else {}
            result = {
                'ok': True, 'draft_id': did,
                'deleted_tracks': out.get('deleted_tracks', []),
                'deleted_count': out.get('deleted_count', 0),
                'duration_s': out.get('duration_sec'),
            }
        else:
            result = {'ok': False, 'draft_id': did, 'error': r.get('error') or str(r)}

    elif name == 'save_draft':
        did = args.get('draft_id') or draft_id
        if not did: return json.dumps({'error': '没有草稿'}, ensure_ascii=False)
        rs._warmup_draft(did)  # 防冷草稿: save_draft 底层也要草稿在缓存里
        r = rs._post_internal('save_draft', {'draft_id': did}, user_id=ctx.uid)
        result = {'ok': r.get('success', False), 'draft_id': did}

    elif name == 'render':
        did = args.get('draft_id') or draft_id
        if not did: return json.dumps({'error': '没有草稿'}, ensure_ascii=False)
        rs._post_internal('save_draft', {'draft_id': did}, user_id=ctx.uid)
        r = rs._post_internal(f'render/draft/{did}', user_id=ctx.uid)
        if r.get('task_id'):
            result = {'task_id': r['task_id'], 'poll': r.get('poll', ''), 'ok': True, 'draft_id': did}
        else:
            result = {'error': str(r)}

    elif name == 'render_status':
        task_id = args.get('task_id', '')
        if args.get('wait'):
            # 服务端阻塞等待: 每次调用最多等 ~25s, 直到状态/阶段变化或终态再返回.
            # 给 agent 自动监控用 —— 没有它 agent 只能连续空转查询 (LLM 侧无法 sleep).
            waited = 0.0
            r = None
            prev = None
            while waited < 25:
                r = rs._get_internal(f"render/status/{task_id}", user_id=ctx.uid)
                cur = (r.get('status'), (r.get('progress') or {}).get('stage')) if isinstance(r, dict) else None
                if not isinstance(r, dict) or r.get('status') in ('done', 'error') or (prev is not None and cur != prev):
                    break
                prev = cur
                time.sleep(2.5)
                waited += 2.5
            result = r if isinstance(r, dict) else {'error': str(r)}
        else:
            r = rs._get_internal(f"render/status/{task_id}", user_id=ctx.uid)
            result = r if isinstance(r, dict) else {'error': str(r)}

    elif name == 'bsk_run':
        cmd = (args.get('command') or '').strip()
        if not cmd:
            result = {'error': 'command required'}
        else:
            try:
                import shlex
                bsk = getattr(rs.config, 'BSK_BIN', None) or 'bsk'
                p = subprocess.run([bsk] + shlex.split(cmd),
                                   capture_output=True, text=True, encoding='utf-8',
                                   errors='replace', timeout=int(args.get('timeout', 60)),
                                   cwd=rs.HERE)
                result = {'ok': p.returncode == 0, 'code': p.returncode,
                          'stdout': (p.stdout or '')[-3000:], 'stderr': (p.stderr or '')[-1500:]}
            except FileNotFoundError:
                result = {'error': 'bsk 未安装 (见 BrowserSkill/AGENT_INSTALL.md)', 'ok': False}
            except subprocess.TimeoutExpired:
                result = {'error': 'bsk 命令超时', 'ok': False}

    elif name == 'list_drafts':
        r = rs._get_internal('api/drafts', user_id=ctx.uid)
        if isinstance(r, list):
            result = {'drafts': [
                {'name': d.get('name'), 'id': d.get('id'), 'duration': d.get('duration'),
                 'modified': d.get('modified')} for d in r
            ]}
        else:
            result = {'error': str(r)}

    elif name == 'get_draft_timeline':
        did = args.get('draft_id') or draft_id
        if not did:
            result = {'error': '当前无激活草稿。先 create_draft 新建或让用户在 Drafts 打开一个草稿。'}
        else:
            r = rs._get_internal(f'api/draft/timeline/{did}', user_id=ctx.uid)
            if not isinstance(r, dict) or not r.get('success'):
                result = {'error': (r.get('error') if isinstance(r, dict) else None) or '读取草稿时间线失败'}
            else:
                try:
                    content = json.loads(r['output'])
                except Exception as e:
                    result = {'error': f'解析草稿 json 失败: {e}'}
                else:
                    # material_id -> 素材摘要 索引 (跨所有素材类型, 仿 TimelinePanel.tsx:86-93)
                    mats = content.get('materials', {}) or {}
                    mat_by_id = {}
                    for mlist in (mats.get('videos'), mats.get('audios'), mats.get('texts'),
                                  mats.get('stickers'), mats.get('images') or mats.get('photos')):
                        for m in (mlist or []):
                            if m.get('id'):
                                mat_by_id[m['id']] = m

                    def _mat_name(m):
                        # video/text/sticker 用 material_name; audio 用 name; 兜底 basename(path)
                        n = m.get('material_name') or m.get('name')
                        if n:
                            return n
                        p = m.get('path') or m.get('media_path') or m.get('remote_url') or ''
                        return os.path.basename(p) if p else '(无名)'

                    def _mat_source_name(m):
                        # 草稿内的 material_name 是 video_<hash>.mp4 这种内部名, agent 无法用它去
                        # 素材库 (list_resources / get_resource_detail, 那里存的是原始文件名如
                        # VID_20260819_102125.mp4) 查内容。这里补出"原始文件名", 让 agent 能直接
                        # 拿它去 get_resource_detail, 不必先 list_resources 再逐个猜对得上。
                        src = m.get('remote_url') or m.get('path') or m.get('media_path') or ''
                        if src:
                            return os.path.basename(src)
                        return None

                    def _mat_text(m):
                        # 文字素材: content 是 {"styles":[...],"text":"..."} 的 json 串
                        c = m.get('content')
                        if not c:
                            return None
                        try:
                            return (json.loads(c).get('text') if isinstance(c, str) else c.get('text')) or None
                        except Exception:
                            return None

                    tracks_out = []
                    for t in (content.get('tracks') or []):
                        segs = t.get('segments') or []
                        # 不再跳过空轨道: agent 必须能看见空轨(如预建未填充的默认
                        # "video" 轨), 才知道该删它。用 is_empty/segment_count 标注。
                        seg_list = []
                        for s in segs:
                            tr = s.get('target_timerange') or {}
                            start_us = tr.get('start', 0)
                            dur_us = tr.get('duration', 0)
                            m = mat_by_id.get(s.get('material_id'), {})
                            seg_list.append({
                                'name': _mat_name(m),
                                'source_name': _mat_source_name(m),  # 原始文件名(素材库里的真名), agent 拿它去 get_resource_detail 查内容; 视/图段有, 文字段无
                                'type': t.get('type'),            # video/audio/text/sticker/...
                                'track': t.get('name'),
                                'start_s': round(start_us / 1_000_000, 3),
                                'end_s': round((start_us + dur_us) / 1_000_000, 3),
                                'duration_s': round(dur_us / 1_000_000, 3),
                                'text': _mat_text(m),             # 仅文字段有值
                            })
                        tracks_out.append({
                            'track': t.get('name'), 'type': t.get('type'),
                            'track_id': t.get('id'),              # 稳定唯一 id, 同名轨道消歧用 (传给 delete_track(track_id=...))
                            'segment_count': len(segs),
                            'is_empty': len(segs) == 0,           # 空轨道标注, agent 一眼看出该删
                            'segments': seg_list,
                        })
                    result = {
                        'draft_id': did,
                        'duration_s': round((content.get('duration') or 0) / 1_000_000, 3),
                        'tracks': tracks_out,
                        'total_segments': sum(t['segment_count'] for t in tracks_out),
                        'source': r.get('source'),   # cache / disk —— 冷草稿时 disk
                    }

    elif name == 'list_templates':
        r = rs._get_internal('api/templates')
        result = {'templates': r} if isinstance(r, list) else {'error': str(r)}

    elif name == 'run_template':
        r = rs._post_internal('api/templates/render', {
            'template': args.get('template', ''),
            'variables': args.get('variables', {}) or {},
            'render': False,
        })
        if r.get('draft_id'):
            draft_id = r['draft_id']
            ctx.draft_id = draft_id
            ctx.on_draft_created(draft_id)
            result = {'ok': True, 'draft_id': draft_id}
        else:
            result = {'error': str(r)}

    else:
        # 模型偶尔会吐出空/未知的 tool_call name (观察到 qwen 多轮工具调用时出现过).
        # 明确报错而不是静默回空 dict, 否则模型会把"空结果"当成"调用成功但没内容"
        # 继而自己编一个看起来合理的答案 —— 这比直接报错更危险.
        return json.dumps({'error': f'未知工具: {name!r}'}, ensure_ascii=False)

    # 确保 result 是 dict, 再序列化
    if not isinstance(result, dict):
        result = {'raw': str(result)[:500]}
    return json.dumps(result, ensure_ascii=False)



def build_tools(ctx: ToolContext):
    """把 TOOL_SCHEMAS 包装成 Agents SDK 的 FunctionTool 列表.
    on_invoke_tool 统一走 execute_tool 单一分发点 (防御逻辑不重复)."""
    tools = []
    for entry in TOOL_SCHEMAS:
        fn = entry['function']
        name = fn['name']

        def _make_invoker(tool_name):
            async def _invoke(wrapper: RunContextWrapper, args_json: str) -> str:
                # SDK 传入 JSON 字符串参数; 解析失败作为错误结果回给模型自纠
                try:
                    parsed = json.loads(args_json or '{}')
                except json.JSONDecodeError as je:
                    return json.dumps({'error': f'工具参数 JSON 解析失败: {je}; 请重新调用并给出完整合法的 JSON 参数'}, ensure_ascii=False)
                try:
                    out = execute_tool(tool_name, parsed, ctx)
                except Exception as te:
                    out = json.dumps({'error': f'工具执行异常: {te}'}, ensure_ascii=False)
                try:
                    ctx.on_tool_executed(tool_name, parsed, out)
                except Exception:
                    pass
                return out
            return _invoke

        tools.append(FunctionTool(
            name=name,
            description=fn.get('description', ''),
            params_json_schema=fn.get('parameters') or {'type': 'object', 'properties': {}},
            on_invoke_tool=_make_invoker(name),
            strict_json_schema=False,  # 旧 schema 未按 strict 规范把全部字段列入 required
        ))
    return tools
