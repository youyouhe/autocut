# 项目元数据 — AI 视频工作台

> 数据来源：`SYSTEM_MANUAL.md`（2026-08-14 版系统功能手册）
> 项目定位：以剪映专业版 5.9.0 为渲染后端，通过 Frida 注入 + 多桌面真后台，实现"上传素材 → AI 感知 → 模板/对话组装草稿 → 无人值守渲染 → AI 质检"全自动生产线的本地 Web 服务。

## 行业领域

| 维度 | 内容 |
|------|------|
| 行业 | 文化传媒 / AI 工具软件 |
| 细分领域 | AI 视频自动生产（AIGC 内容创作工具链） |
| 业务特征 | 流程驱动（素材→感知→编辑→渲染→质检流水线）、强依赖外部软件（剪映逆向）、本地单机部署、无人值守自动化 |
| 监管要求 | 无行业强制合规；安全要求：API Key 须从硬编码迁移至环境变量（手册 §3.3/§18.4 明示） |
| 信息化现状 | 全新建设；渲染后端复用已安装的剪映专业版 5.9.0（无官方 API，靠逆向驱动） |

## 系统清单

### 分域规划

| 分册 | 业务领域 | 包含系统 | 系统编号 |
|------|---------|---------|---------|
| 第一册 | 素材接入与智能感知 | 素材接入与缓存管理、视频感知与质检 | S01-S02 |
| 第二册 | 内容生产 | 模板引擎、对话式生产 | S03-S04 |
| 第三册 | 草稿编辑与渲染 | 草稿编辑服务（VectCutAPI）、无人值守渲染 | S05-S06 |
| 第四册 | 服务集成与交互 | 核心服务融合与前端 GUI、MCP 工具服务 | S07-S08 |

### 系统功能点统计

| 系统编号 | 系统名称 | 功能点数 | ▲标注数 | 所属分册 |
|---------|---------|---------|---------|---------|
| S01 | 素材接入与缓存管理 | 9 | 0 | 第一册 |
| S02 | 视频感知与质检 | 7 | 0 | 第一册 |
| S03 | 模板引擎 | 10 | 0 | 第二册 |
| S04 | 对话式生产 | 5 | 0 | 第二册 |
| S05 | 草稿编辑服务 | 26 | 0 | 第三册 |
| S06 | 无人值守渲染 | 14 | 0 | 第三册 |
| S07 | 核心服务融合与前端 GUI | 10 | 0 | 第四册 |
| S08 | MCP 工具服务 | 4 | 0 | 第四册 |
| **合计** | | **85** | **0** | |

> 注：数据源为系统功能手册而非标书响应表，无 ▲（重点响应）标注，全部功能点按 P1（核心）或 P2（支撑）定级。

### 系统拆分计划

| 系统编号 | 系统名称 | 功能点数 | 子模块数 | 分级 | Phase 1 输出方式 |
|---------|---------|---------|---------|------|-----------------|
| S01 | 素材接入与缓存管理 | 9 | 0 | 中型 | 单文件，按功能域分批编写 |
| S02 | 视频感知与质检 | 7 | 0 | 中型 | 单文件，按功能域分批编写 |
| S03 | 模板引擎 | 10 | 0 | 中型 | 单文件，按功能域分批编写 |
| S04 | 对话式生产 | 5 | 0 | 小型 | 单文件，一次性完成 |
| S05 | 草稿编辑服务 | 26 | 4 | 大型 | 目录拆分，每子模块独立文件 |
| S06 | 无人值守渲染 | 14 | 3 | 大型 | 目录拆分，每子模块独立文件 |
| S07 | 核心服务融合与前端 GUI | 10 | 0 | 中型 | 单文件，按功能域分批编写 |
| S08 | MCP 工具服务 | 4 | 0 | 小型 | 单文件，一次性完成 |

大型系统子模块划分：

**S05 草稿编辑服务**
| 子模块 | 名称 | 功能点范围 | 内容 |
|--------|------|-----------|------|
| S05-M01 | 草稿生命周期 | S05-001~004 | create_draft / save_draft / query_script / query_draft_status / generate_draft_url |
| S05-M02 | 素材轨道添加 | S05-005~010 | add_video / add_audio / add_image / add_text / add_subtitle / add_sticker |
| S05-M03 | 效果与关键帧 | S05-011~013 | add_effect / add_video_keyframe / generate_draft_url 归属见 M01 |
| S05-M04 | 资源类型字典 | S05-014~026 | 11 个动画/转场/蒙版/字体/特效列表 GET 端点 |

**S06 无人值守渲染**
| 子模块 | 名称 | 功能点范围 | 内容 |
|--------|------|-----------|------|
| S06-M01 | 渲染任务调度 | S06-001~005 | zip 直传渲染 / draft_id 渲染 / 任务生命周期 / 多桌面池 / 渲染 API |
| S06-M02 | 渲染驱动闭环 | S06-006~011 | 草稿注入 / 真后台桌面 / 渲染流程闭环 / 完成检测 / 草稿清理 / 坐标校准 |
| S06-M03 | Frida 注入层 | S06-012~014 | 合成事件 RPC / modal 检测与窗口跟踪 / 渲染监视器 |

## 角色定义

| 角色编码 | 角色名称 | 说明 | 关联系统 |
|---------|---------|------|---------|
| R01 | 视频创作者 | 通过浏览器 Web GUI 上传/接收素材、发起分析、对话式生产、提交渲染、下载成片的普通用户 | S01,S02,S03,S04,S06,S07 |
| R02 | 外部 AI Agent | 通过 MCP stdio 接入的智能体客户端（Claude Code、Cursor 等），调用感知/编辑/渲染工具完成自动化生产 | S08,S01,S02,S05,S06 |
| R03 | 脚本/程序调用方 | 通过 curl/HTTP 脚本直接调用 REST API 的自动化流程（如 zip 直传渲染流水线） | S01,S02,S03,S05,S06 |
| R04 | 系统运维/开发者 | 负责服务启停、坐标校准、配置管理、故障排查（多播网卡、端口冲突、Frida 注入异常）的维护人员 | S06,S07 |
| R05 | 素材发送设备 | 通过 LocalSend App 向系统发送素材的手机/电脑（系统用户侧设备角色，非人员） | S01 |

## 领域术语

| 术语 | 全称/英文 | 定义 | 关联系统 |
|------|---------|------|---------|
| 草稿 | Draft | 剪映工程单元，文件夹含 draft_content.json（时间线内容）与 draft_meta_info.json（元信息） | S05,S06 |
| 草稿注入 | Draft Injection | 复制草稿文件夹进剪映草稿根目录并改 id/name/tm_draft_modified，使剪映首页 ~2s 内识别为第一张卡片 | S06 |
| 真后台 | True Background | 通过 Win32 CreateDesktopW 让剪映运行在独立 Windows 桌面，用户主桌面无感的渲染方式 | S06 |
| 多桌面池 | Desktop Pool | JYRender_0/JYRender_1 两个独立桌面 + 互斥锁 + 阻塞队列，实现 2 路渲染并行 | S06 |
| Frida | — | 动态插桩框架，attach 剪映进程注入 hook_focus.js，实现合成鼠标/键盘事件 | S06 |
| 合成事件 | Synthesized Event | 经 QWindowSystemInterface::handleMouseEvent/handleKeyEvent 注入 Qt 事件循环的程序化点击/输入 | S06 |
| focusWindow | QGuiApplication::focusWindow | Qt 导出函数，返回当前聚焦 QWindow，自动跟踪 home→editor→dialog 切换 | S06 |
| clickmodal | — | 通过 hook QWindow::setVisible 记录的 lastShownWin（最近显示 modal 窗口）执行点击 | S06 |
| dev | QPointingDevice | 程序化获取的鼠标设备指针（primaryPointingDevice），合成事件的必要参数 | S06 |
| 校准 | Calibration | 引导点击记录 card/export/confirm/home 等按钮 local 坐标到 calib.json（1280×720 基准） | S06 |
| local 坐标 | Local Coordinates | 相对窗口原点的坐标（global = origin + local），随窗口移动稳定 | S06 |
| VLM | Vision Language Model | 视觉语言模型（通义千问 qwen3.7-plus），负责画面分析、质检打分、对话 | S02,S04 |
| ASR | Automatic Speech Recognition | 自动语音识别（自建服务），输出词级时间戳转录 | S02 |
| SSE | Server-Sent Events | 服务端推送流式协议，/api/chat 对话流式返回采用 | S04 |
| MCP | Model Context Protocol | 模型上下文协议，将系统能力暴露为 AI 工具（stdio 传输） | S08 |
| Function Calling | — | LLM 工具调用机制，对话式生产中由 LLM 决策调用 create_draft/add_video 等编辑工具 | S04 |
| LocalSend | — | 开源局域网文件传输协议 v2.2（UDP 多播发现 + 明文 HTTP 传输，端口 53317） | S01 |
| 多播 announce | Multicast Announce | 向 224.0.0.167:53317 周期广播设备存在，供 LocalSend 客户端发现 | S01 |
| 场景检测 | Scene Detection | FFmpeg scene 滤镜检测视频镜头切换时间点 | S02 |
| 模板 | Template | YAML 声明式视频模板：canvas + scenes[] + render，经变量填充自动组装草稿 | S03 |
| Scene 处理器 | Scene Handler | 模板引擎中按 type（video/image/text/audio/subtitle）分发的组装逻辑 | S03 |
| 变量填充 | Variable Filling | 递归替换模板中 {{var}} 占位符（str/dict/list 皆可） | S03 |
| overlay | — | 模板场景属性，叠加层不推进时间游标（如标题浮于视频之上） | S03 |
| VectCutAPI | — | 基于 pyJianYingDraft 的草稿编辑 REST 服务（capcut_server.py，28 端点），被 render_server 融合挂载 | S05,S07 |
| pyJianYingDraft | — | 本地 Python 包，程序化生成剪映草稿文件 | S05 |
| draft profile | — | VectCutAPI 草稿格式档位：capcut_legacy / jianying_legacy / jianying_pro_10 | S05 |
| 质检 | Quality Check | 渲染完成后抽 8 帧由 VLM 打分（quality_score/issues/suggestions） | S02 |
| LRU 淘汰 | Least Recently Used | 视频字节缓存（上限 500MB）淘汰最久未访问条目的策略 | S01 |
| root_meta_info.json | — | 剪映草稿根目录的全局草稿索引，注入草稿清理时须同步移除条目 | S06 |
| 等保 | — | 无适用项（本系统为本地单机工具，无等级保护要求） | — |

## 外部系统清单

| 编码 | 系统名称 | 系统类型 | 对接方式 | 说明 |
|------|---------|---------|---------|------|
| EXT-01 | 剪映专业版 JianyingPro 5.9.0 | 渲染后端（桌面软件） | Frida 注入 + Win32 多桌面 + 文件系统 | 无官方 API；逆向 Qt6 内部函数驱动 UI 完成导出 |
| EXT-02 | 通义千问 qwen3.7-plus | VLM/LLM 云服务 | REST（OpenAI 兼容，dashscope） | 画面分析、对话 function calling、质检打分 |
| EXT-03 | 自建 ASR 服务 | 语音识别服务 | REST（HTTPS，asr.smartbid.site/inference） | 语音转文字，词级时间戳 |
| EXT-04 | LocalSend 客户端 App | 移动端/桌面传输工具 | UDP 多播 + HTTP（v2.2 协议，端口 53317） | 手机/电脑向系统发送素材 |
| EXT-05 | FFmpeg / FFprobe | 媒体处理工具链 | 命令行子进程 | 元数据提取、抽帧、场景检测、音频提取 |
| EXT-06 | MCP 客户端（Claude Code 等） | AI Agent 宿主 | MCP stdio | 经 mcp_video_server.py 调用系统能力 |

## 合规要求

| 类别 | 标准/要求 | 影响范围 | 说明 |
|------|---------|---------|------|
| 版本锁定 | 剪映 JianyingPro 5.9.0.11632 | 全局（S06 强相关） | Frida 按 mangled 导出名动态查找，跨版本需回归验证；剪映大版本升级可能破坏注入链路 |
| 密钥安全 | API Key 环境变量化 | S02,S04 | Qwen/ASR Key 当前硬编码于 perceive.py / render_server.py，生产部署须迁移 |
| 操作系统约束 | Windows 10 Pro 19045 + Python 3.12 | 全局 | 依赖 Win32 API（CreateDesktopW）与剪映 Windows 版 |
| 运行环境 | 端口 9002（主服务）/ 53317（LocalSend）/ 5173（前端 dev） | 全局 | 端口冲突时启动失败/降级 |
| 校准基准 | 窗口 1280×720 | S06 | 分辨率/DPI 变化须重新 calibrate |

## 数据类型约定

存储形态：**无关系型数据库**。任务/会话为内存字典（线程锁保护），分析结果为 JSON 文件（analysis_cache/*.json），草稿为文件系统（draft_content.json / draft_meta_info.json），配置为 JSON（calib.json / .mcp.json / VectCutAPI/config.json）。

| 场景 | 数据类型 | 长度/精度 | 说明 |
|------|---------|----------|------|
| 任务ID task_id | VARCHAR(Hex) | 8 | uuid hex 前 8 位 |
| 草稿ID draft_id | VARCHAR(UUID) | 36 | 注入时生成新 UUID |
| 注入草稿名 | VARCHAR | 32 | rd<毫秒时间戳>，纯英文数字（避免 IME/QString 问题） |
| 文件路径 | VARCHAR | 260 | Windows MAX_PATH 约定，正斜杠/反斜杠均接受 |
| 时间戳 tm_draft_modified | BIGINT | 毫秒 | Unix 毫秒时间戳（剪映草稿元数据约定） |
| 视频时长 duration | DECIMAL | (10,3) | 秒，ffprobe 提取 |
| 分辨率 width/height | INT | 4 | 像素，默认画布 1080×1920 |
| 帧率 fps | DECIMAL | (5,2) | |
| 状态枚举（任务） | VARCHAR | 10 | queued / rendering / done / error |
| 状态枚举（监视器） | VARCHAR | 10 | idle / rendering / done |
| 质量分 quality_score | DECIMAL | (3,1) | 0-10，VLM 打分 |
| 时间区间（场景/ASR segment） | DECIMAL | (10,3) | start/end 秒 |
| 布尔 | TINYINT/JSON bool | 1 | false/true（JSON 文件内为 bool） |
| 文件大小阈值 | INT | MB | 视频 ≤50MB 入内存缓存；总上限 500MB |
| 校准坐标 lx/ly | INT | 4 | 1280×720 local 坐标系 |
| 内存缓存键 | VARCHAR(MD5) | 32 | 素材路径 md5 |
| LLM 消息 | TEXT | - | SSE data 事件 JSON |
| 长文本（转录/分析） | TEXT | - | full_text / visual_analysis JSON 字符串 |

## 功能原型分类

| 原型 | 说明 | 匹配的功能点 |
|------|------|------------|
| A 数据录入/维护 | CRUD + 校验 + 同步 | S01-005~009, S05-001~004, S07-001~002, S07-008~010 |
| B 评估/量表/评分 | 评估项 + 自动计算 + 等级判定 | S02-003, S02-006（VLM 打分/质检） |
| C 文书/报告/表单 | 模板 + 填写 + 组装 + 输出 | S03-001~010（YAML 模板→草稿组装） |
| D 统计/分析/报表 | 维度 + 指标 + 聚合 + 可视化 | S02-001~002, S02-004~005, S01-008~009, S05-014~026 |
| E 流程/闭环/审批 | 状态机 + 节点操作 + 校验 + 追踪 | S06-001~011, S04-001~005, S01-001~004 |
| F 管理/配置/权限 | 配置项 + 规则 + 生效条件 | S06-012~014, S07-001~002 |
| G 集成/同步/对接 | 数据映射 + 协议转换 + 监控 | S01-001~004, S06-006~008, S08-001~004 |
| H 移动端/扫码 | 离线 + 扫码 + 推送 + 轻量交互 | S01-001~004（LocalSend 手机发送侧配合）, S07-003~007 |

## 各系统功能点索引（供 Phase 1 逐系统展开）

### S01 素材接入与缓存管理（9 点）
| 功能编号 | 功能名称 | 功能原型 | 优先级 |
|---------|---------|---------|--------|
| S01-001 | LocalSend 设备发现（UDP 多播 announce） | G/H | P1 |
| S01-002 | LocalSend 上传传输（prepare-upload/upload 流式接收） | G/H | P1 |
| S01-003 | LocalSend 会话管理（单会话/SHA256/重名序号） | E | P1 |
| S01-004 | LocalSend 接收端启停与状态查询 | E/F | P1 |
| S01-005 | Web 文件上传（自动分类 video/image/audio） | A | P1 |
| S01-006 | 素材库扫描（render_uploads/ 全量素材） | A | P1 |
| S01-007 | 视频字节内存缓存（≤50MB 入 RAM，LRU 500MB） | A | P2 |
| S01-008 | 分析元数据缓存（md5 键 O(1) 查询） | D | P1 |
| S01-009 | 内存与统计查询（/api/memory/*） | D | P2 |

### S02 视频感知与质检（7 点）
| 功能编号 | 功能名称 | 功能原型 | 优先级 |
|---------|---------|---------|--------|
| S02-001 | 视频元数据提取（ffprobe：时长/分辨率/帧率） | D | P1 |
| S02-002 | 场景检测（FFmpeg scene 滤镜） | D | P1 |
| S02-003 | 抽帧与 VLM 画面分析（内容/情绪/质量/亮点/帧内文字） | B/D | P1 |
| S02-004 | ASR 语音转录（词级时间戳） | D | P1 |
| S02-005 | ASR 响应解析归一化（JSON/SRT/包装格式兼容） | G | P1 |
| S02-006 | 渲染质检（抽 8 帧 VLM 打分：quality_score/issues/suggestions） | B | P1 |
| S02-007 | 感知 API（路径分析/缓存查询/上传分析/质检端点） | A/D | P1 |

### S03 模板引擎（10 点）
| 功能编号 | 功能名称 | 功能原型 | 优先级 |
|---------|---------|---------|--------|
| S03-001 | YAML 模板定义解析（canvas/scenes/render） | C | P1 |
| S03-002 | 变量填充（{{var}} 递归替换） | C | P1 |
| S03-003 | video 场景处理器 | C | P1 |
| S03-004 | image 场景处理器 | C | P1 |
| S03-005 | text 场景处理器（样式/位置/动画） | C | P1 |
| S03-006 | audio 场景处理器 | C | P1 |
| S03-007 | subtitle 场景处理器（items 批量字幕） | C | P1 |
| S03-008 | overlay 叠加层与时间游标控制 | C | P1 |
| S03-009 | 模板执行流程（组装→保存→可选渲染） | E/C | P1 |
| S03-010 | 内置模板库与模板 API（list/render） | C/F | P2 |

### S04 对话式生产（5 点）
| 功能编号 | 功能名称 | 功能原型 | 优先级 |
|---------|---------|---------|--------|
| S04-001 | SSE 流式对话（/api/chat） | E | P1 |
| S04-002 | 资源查询工具（list_resources/get_resource_detail/get_transcript） | E | P1 |
| S04-003 | 编辑工具链（create_draft/add_video/add_text/save_draft） | E | P1 |
| S04-004 | 渲染工具（render/render_status） | E | P1 |
| S04-005 | 草稿上下文关联（draft_id 贯穿多轮对话） | E | P1 |

### S05 草稿编辑服务（26 点）
| 功能编号 | 功能名称 | 功能原型 | 优先级 | 子模块 |
|---------|---------|---------|--------|--------|
| S05-001 | 创建草稿 create_draft | A | P1 | M01 |
| S05-002 | 保存草稿 save_draft | A | P1 | M01 |
| S05-003 | 查询草稿脚本 query_script | D | P1 | M01 |
| S05-004 | 查询草稿状态 query_draft_status | D | P1 | M01 |
| S05-005 | 生成草稿 URL generate_draft_url | A | P2 | M01 |
| S05-006 | 添加视频轨道 add_video（转场/蒙版/变速/音量） | A | P1 | M02 |
| S05-007 | 添加音频轨道 add_audio | A | P1 | M02 |
| S05-008 | 添加图片 add_image | A | P1 | M02 |
| S05-009 | 添加文字 add_text（字体/颜色/阴影/背景/动画） | A | P1 | M02 |
| S05-010 | 批量字幕 add_subtitle | A | P1 | M02 |
| S05-011 | 添加贴纸 add_sticker | A | P1 | M02 |
| S05-012 | 添加特效 add_effect | A | P1 | M03 |
| S05-013 | 视频关键帧 add_video_keyframe | A | P1 | M03 |
| S05-014 | 入场动画列表 get_intro_animation_types | D | P2 | M04 |
| S05-015 | 出场动画列表 get_outro_animation_types | D | P2 | M04 |
| S05-016 | 组合动画列表 get_combo_animation_types | D | P2 | M04 |
| S05-017 | 转场列表 get_transition_types | D | P2 | M04 |
| S05-018 | 蒙版列表 get_mask_types | D | P2 | M04 |
| S05-019 | 音效列表 get_audio_effect_types | D | P2 | M04 |
| S05-020 | 字体列表 get_font_types | D | P2 | M04 |
| S05-021 | 文字入场列表 get_text_intro_types | D | P2 | M04 |
| S05-022 | 文字出场列表 get_text_outro_types | D | P2 | M04 |
| S05-023 | 文字循环动画列表 get_text_loop_anim_types | D | P2 | M04 |
| S05-024 | 视频场景特效列表 get_video_scene_effect_types | D | P2 | M04 |
| S05-025 | 视频人物特效列表 get_video_character_effect_types | D | P2 | M04 |
| S05-026 | 草稿格式配置（draft_profile/IS_CAPCUT_ENV） | F | P2 | M04 |

### S06 无人值守渲染（14 点）
| 功能编号 | 功能名称 | 功能原型 | 优先级 | 子模块 |
|---------|---------|---------|--------|--------|
| S06-001 | 渲染任务提交（zip 直传 /render） | E | P1 | M01 |
| S06-002 | 按草稿 ID 渲染（/render/draft/<id>） | E | P1 | M01 |
| S06-003 | 任务生命周期管理（queued/rendering/done/error + 产物登记） | E | P1 | M01 |
| S06-004 | 多桌面渲染池（2 桌面互斥 + 阻塞队列 worker） | E/F | P1 | M01 |
| S06-005 | 渲染状态/下载/列表 API | D | P1 | M01 |
| S06-006 | 草稿注入（inject_draft：复制/改 id/name/排首页第一） | G | P1 | M02 |
| S06-007 | 真后台桌面隔离（CreateDesktopW + lpDesktop 启动剪映） | G | P1 | M02 |
| S06-008 | 渲染流程闭环（card→export→confirm→done→close，含 4/5 次重试） | E | P1 | M02 |
| S06-009 | 完成检测（轮询 Videos/*.mp4 大小稳定，非 temp） | E | P1 | M02 |
| S06-010 | 草稿清理与 root_meta 同步（防"草稿丢失"弹窗） | E | P1 | M02 |
| S06-011 | 坐标校准（calibrate：card/export/confirm/close 等 8 坐标） | F | P1 | M02 |
| S06-012 | Frida 合成事件层（click/clickmodal/typewm/状态 RPC） | G | P1 | M03 |
| S06-013 | modal 检测与窗口跟踪（setVisible hook + focusWindow） | G | P1 | M03 |
| S06-014 | 渲染监视器（render_monitor.py 文件轮询状态机） | D | P2 | M03 |

### S07 核心服务融合与前端 GUI（10 点）
| 功能编号 | 功能名称 | 功能原型 | 优先级 |
|---------|---------|---------|--------|
| S07-001 | VectCutAPI 融合机制（sys.path 挂载 + 降级模式） | G/F | P1 |
| S07-002 | 服务启停管理（serve.bat/stop.bat/端口检测/PID） | F | P1 |
| S07-003 | 素材面板（上传/LocalSend/分析/预览） | H | P1 |
| S07-004 | LocalSend 接收对话框（启停/状态/已收列表） | H | P1 |
| S07-005 | 对话面板（SSE 流式渲染） | H | P1 |
| S07-006 | 草稿管理面板（列表/封面/删除） | H | P1 |
| S07-007 | 渲染任务面板（提交/状态/下载） | H | P1 |
| S07-008 | 草稿管理 API（/api/drafts 列表/封面/删除 + root_meta 同步） | A | P1 |
| S07-009 | 静态托管与 SPA 路由（Vue catch-all + CORS） | F | P2 |
| S07-010 | 健康检查（/health） | F | P2 |

### S08 MCP 工具服务（4 点）
| 功能编号 | 功能名称 | 功能原型 | 优先级 |
|---------|---------|---------|--------|
| S08-001 | MCP Server 配置与 stdio 传输（.mcp.json） | G | P1 |
| S08-002 | 感知工具（perceive_video/perceive_result） | G | P1 |
| S08-003 | 编辑与渲染工具（create_draft/add_video/add_text/add_audio/save_draft/render/render_status） | G | P1 |
| S08-004 | 资源列表工具（get_animations：intro/outro/combo/transition/mask/font/effect） | G | P2 |

---
*元数据生成日期：2026-08-14　数据源：SYSTEM_MANUAL.md（唯一数据源，无标书响应表）*
