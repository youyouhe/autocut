# mcp_video_server.py — MCP Server: 把 render_server 的能力暴露为 MCP 工具
# 任何 MCP 客户端 (Claude Code / Pi / Cursor) 可直接调用
# 工具: perceive_video, create_draft, add_video, add_text, save_draft, render, perceive_result
import os, sys, json, asyncio, subprocess, tempfile, base64
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config

RENDER_SERVER = config.API_BASE

app = Server("video-tools")


def _post(endpoint, **kwargs):
    """调 render_server REST API"""
    url = f"{RENDER_SERVER}/{endpoint.lstrip('/')}"
    r = requests.post(url, timeout=600, **kwargs)
    return r.json()


def _get(endpoint):
    r = requests.get(f"{RENDER_SERVER}/{endpoint.lstrip('/')}", timeout=30)
    return r.json()


@app.list_tools()
async def list_tools():
    return [
        Tool(
            name="perceive_video",
            description="让 AI 看懂一个视频文件的内容。返回：画面描述、情绪、质量评分、精彩片段、适合用途。需要视频路径。",
            inputSchema={
                "type": "object",
                "properties": {
                    "video_path": {"type": "string", "description": "视频文件路径"},
                    "do_asr": {"type": "boolean", "description": "是否做语音转文字", "default": True},
                },
                "required": ["video_path"]
            }
        ),
        Tool(
            name="create_draft",
            description="创建一个新的剪映/CapCut 草稿。返回 draft_id。",
            inputSchema={
                "type": "object",
                "properties": {
                    "width": {"type": "integer", "description": "视频宽度", "default": 1080},
                    "height": {"type": "integer", "description": "视频高度", "default": 1920},
                }
            }
        ),
        Tool(
            name="add_video",
            description="向草稿添加视频轨道。支持转场、蒙版、变速、音量。",
            inputSchema={
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                    "video_url": {"type": "string", "description": "视频URL"},
                    "start": {"type": "number", "description": "源视频开始时间(秒)", "default": 0},
                    "end": {"type": "number", "description": "源视频结束时间(秒)"},
                    "target_start": {"type": "number", "description": "时间轴位置(秒)", "default": 0},
                    "volume": {"type": "number", "description": "音量0-1", "default": 1.0},
                    "transition": {"type": "string", "description": "转场类型"},
                },
                "required": ["draft_id", "video_url"]
            }
        ),
        Tool(
            name="add_text",
            description="向草稿添加文字。支持字体、颜色、大小、阴影、背景、动画。",
            inputSchema={
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                    "text": {"type": "string", "description": "文字内容"},
                    "start": {"type": "number", "description": "开始时间(秒)"},
                    "end": {"type": "number", "description": "结束时间(秒)"},
                    "font_size": {"type": "number", "default": 10},
                    "font_color": {"type": "string", "default": "#FFFFFF"},
                    "transform_y": {"type": "number", "description": "Y位置(-1到1)", "default": 0},
                    "transform_x": {"type": "number", "description": "X位置(-1到1)", "default": 0},
                },
                "required": ["draft_id", "text", "start", "end"]
            }
        ),
        Tool(
            name="add_audio",
            description="向草稿添加音频轨道。",
            inputSchema={
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                    "audio_url": {"type": "string", "description": "音频URL"},
                    "start": {"type": "number", "default": 0},
                    "end": {"type": "number"},
                    "volume": {"type": "number", "default": 1.0},
                },
                "required": ["draft_id", "audio_url"]
            }
        ),
        Tool(
            name="save_draft",
            description="保存草稿到剪映草稿目录，生成可渲染的草稿文件夹。",
            inputSchema={
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                    "draft_folder": {"type": "string", "description": "草稿输出目录(默认剪映草稿目录)"},
                },
                "required": ["draft_id"]
            }
        ),
        Tool(
            name="render",
            description="渲染草稿为 mp4 视频（真后台，剪映在独立桌面运行，不打扰用户）。返回 task_id，用 render_status 查进度。",
            inputSchema={
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string", "description": "草稿ID（先 save_draft 生成）"},
                },
                "required": ["draft_id"]
            }
        ),
        Tool(
            name="render_status",
            description="查询渲染任务状态。status=done 后可用 render_download 下载。",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                },
                "required": ["task_id"]
            }
        ),
        Tool(
            name="perceive_result",
            description="让 AI 检查渲染出的视频质量。返回质量评分、问题列表、改进建议。",
            inputSchema={
                "type": "object",
                "properties": {
                    "video_path": {"type": "string", "description": "渲染出的 mp4 路径"},
                    "expectations": {"type": "string", "description": "质检标准(可选)"},
                },
                "required": ["video_path"]
            }
        ),
        Tool(
            name="get_animations",
            description="获取可用的动画/转场/特效类型列表。",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["intro", "outro", "combo", "transition", "mask", "font", "effect"]},
                },
                "required": ["category"]
            }
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "perceive_video":
            # perceive_video 需要 multipart 上传文件
            video_path = arguments["video_path"]
            do_asr = arguments.get("do_asr", True)
            with open(video_path, "rb") as f:
                result = _post("perceive/video", files={"video": f},
                               data={"do_asr": str(do_asr).lower()})
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        elif name == "create_draft":
            result = _post("create_draft", json={
                "width": arguments.get("width", 1080),
                "height": arguments.get("height", 1920),
            })
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "add_video":
            result = _post("add_video", json=arguments)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "add_text":
            result = _post("add_text", json=arguments)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "add_audio":
            result = _post("add_audio", json=arguments)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "save_draft":
            result = _post("save_draft", json=arguments)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "render":
            draft_id = arguments["draft_id"]
            result = _post(f"render/draft/{draft_id}")
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "render_status":
            result = _get(f"render/status/{arguments['task_id']}")
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "perceive_result":
            video_path = arguments["video_path"]
            expectations = arguments.get("expectations")
            with open(video_path, "rb") as f:
                data = {}
                if expectations:
                    data["expectations"] = expectations
                result = _post("perceive/result", files={"video": f}, data=data)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "get_animations":
            cat_map = {
                "intro": "get_intro_animation_types",
                "outro": "get_outro_animation_types",
                "combo": "get_combo_animation_types",
                "transition": "get_transition_types",
                "mask": "get_mask_types",
                "font": "get_font_types",
                "effect": "get_video_scene_effect_types",
            }
            endpoint = cat_map.get(arguments["category"], "get_intro_animation_types")
            result = _get(endpoint)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        else:
            return [TextContent(type="text", text=f"未知工具: {name}")]

    except Exception as e:
        return [TextContent(type="text", text=f"工具执行错误: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
