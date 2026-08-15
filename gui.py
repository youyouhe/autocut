# gui.py — AI 视频编辑工作台
# 导入资源 → 预览 → 跟 Agent 对话 → 自动编辑 → 渲染 → 下载
#
# 启动: python gui.py
# 打开: http://localhost:7860

import os, sys, json, time, tempfile, shutil
import gradio as gr
import requests

try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

from openai import OpenAI
from perceive import QWEN_API_KEY, QWEN_BASE_URL, QWEN_MODEL, perceive_video, perceive_result
import config

HERE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = config.GUI_UPLOAD_DIR
os.makedirs(UPLOAD_DIR, exist_ok=True)

API_BASE = config.API_BASE
LLM_MODEL = QWEN_MODEL
llm = OpenAI(api_key=QWEN_API_KEY, base_url=QWEN_BASE_URL)

# 全局状态
session = {
    'assets': [],        # [{path, name, type, analysis}]
    'draft_id': None,    # 当前草稿ID
    'render_task': None, # 渲染任务ID
}


def api_post(endpoint, data=None, files=None):
    try:
        r = requests.post(f"{API_BASE}/{endpoint}", json=data, files=files, timeout=600)
        return r.json()
    except Exception as e:
        return {'error': str(e)}


def api_get(endpoint):
    try:
        return requests.get(f"{API_BASE}/{endpoint}", timeout=30).json()
    except Exception as e:
        return {'error': str(e)}


# ============================================================ 资源管理

def on_upload(files):
    """处理上传的文件"""
    results = []
    for f in files:
        name = os.path.basename(f.name) if hasattr(f, 'name') else str(f)
        dst = os.path.join(UPLOAD_DIR, name)
        try:
            shutil.copy2(f.name, dst)
        except:
            dst = f.name if hasattr(f, 'name') else str(f)

        ext = os.path.splitext(name)[1].lower()
        ftype = 'video' if ext in ('.mp4', '.mov', '.avi', '.mkv') else \
                'image' if ext in ('.jpg', '.png', '.jpeg', '.webp') else \
                'audio' if ext in ('.mp3', '.wav', '.aac', '.m4a') else 'other'

        asset = {'path': dst, 'name': name, 'type': ftype, 'analysis': None}
        session['assets'].append(asset)
        results.append(f"✅ {name} ({ftype})")

    asset_list = "\n".join(results)
    preview_html = build_asset_preview()
    return f"已导入 {len(files)} 个资源:\n{asset_list}", preview_html, gr.update(choices=[a['name'] for a in session['assets']])


def build_asset_preview():
    """构建资源预览 HTML"""
    if not session['assets']:
        return "<p style='color:#888;text-align:center;padding:40px'>暂无资源。请上传视频/图片/音频。</p>"

    html = "<div style='display:flex;flex-wrap:wrap;gap:12px;padding:8px'>"
    for a in session['assets']:
        icon = {'video': '🎬', 'image': '🖼️', 'audio': '🎵', 'other': '📄'}.get(a['type'], '📄')
        color = {'video': '#4ECDC4', 'image': '#FFD700', 'audio': '#FF6B6B'}.get(a['type'], '#888')
        analyzed = "✓ 已分析" if a.get('analysis') else "未分析"
        html += f"""
        <div style='width:140px;border:1px solid #333;border-radius:8px;padding:8px;background:#1a1a2e'>
            <div style='font-size:32px;text-align:center'>{icon}</div>
            <div style='font-size:12px;color:{color};text-align:center;margin-top:4px'>{a['type']}</div>
            <div style='font-size:11px;color:#ccc;margin-top:4px;word-break:break-all'>{a['name'][:20]}</div>
            <div style='font-size:10px;color:#666;margin-top:2px'>{analyzed}</div>
        </div>"""
    html += "</div>"
    return html


def analyze_asset(asset_name):
    """分析选中的资源"""
    asset = next((a for a in session['assets'] if a['name'] == asset_name), None)
    if not asset:
        return "未找到资源", ""

    if asset['type'] not in ('video',):
        return f"{asset_name} 不是视频，跳过分析", build_asset_preview()

    yield f"🔍 正在分析 {asset_name}...", gr.update()

    result = perceive_video(asset['path'], do_asr=True, frame_count=4)
    asset['analysis'] = result

    summary = format_analysis(result)
    yield f"✅ {asset_name} 分析完成:\n\n{summary}", build_asset_preview()


def format_analysis(result):
    """格式化分析结果"""
    lines = []
    meta = result.get('meta', {})
    lines.append(f"📐 {meta.get('width')}x{meta.get('height')}, {meta.get('duration', 0):.1f}秒")

    visual = result.get('visual_analysis', '')
    if visual:
        # 提取 JSON 里的关键字段
        try:
            start = visual.find('{')
            end = visual.rfind('}') + 1
            if start >= 0:
                data = json.loads(visual[start:end])
                lines.append(f"🎬 内容: {data.get('content', '')}")
                lines.append(f"🎭 情绪: {data.get('mood', '')}")
                lines.append(f"⭐ 质量: {data.get('quality', '')}")
                highlights = data.get('highlights', [])
                if highlights:
                    lines.append(f"🔥 精彩: {'; '.join(highlights)}")
                suitable = data.get('suitable_for', [])
                if suitable:
                    lines.append(f"💡 适合: {', '.join(suitable)}")
        except:
            lines.append(f"🎬 {visual[:200]}")

    audio = result.get('audio', {})
    if isinstance(audio, dict) and audio.get('full_text'):
        lines.append(f"🔊 语音: {audio['full_text'][:100]}")

    return '\n'.join(lines)


# ============================================================ Agent 对话

def chat_agent(message, history):
    """Agent 对话核心"""
    if not message.strip():
        yield history, ""
        return

    # 构建上下文
    asset_summary = "\n".join([
        f"- {a['name']} ({a['type']}): {(a.get('analysis', {}).get('visual_analysis', '') if a.get('analysis') else '未分析')[:100]}"
        for a in session['assets']
    ]) or "暂无资源"

    system_prompt = f"""你是一个 AI 视频编辑助手。用户会告诉你想做什么视频，你帮他们规划并执行。

当前资源:
{asset_summary}

当前草稿: {session.get('draft_id', '无')}

你可以调用以下能力（通过特殊的函数调用格式）:
- CREATE_DRAFT(width, height) — 创建草稿
- ADD_VIDEO(draft_id, url, start, end) — 添加视频
- ADD_TEXT(draft_id, text, start, end, font_size, color) — 添加文字
- SAVE_DRAFT(draft_id) — 保存草稿
- RENDER(draft_id) — 渲染
- ANALYZE(asset_name) — 分析资源

当需要执行操作时，输出 ACTION: 开头的行。我会替你执行。
对于规划/建议，直接用自然语言回复。保持简洁。"""

    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-10:]:
        messages.append({"role": "user", "content": h[0]})
        if h[1]:
            messages.append({"role": "assistant", "content": h[1]})
    messages.append({"role": "user", "content": message})

    # 调 LLM
    yield history + [[message, None]], "思考中..."

    try:
        r = llm.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            max_tokens=1000,
        )
        reply = r.choices[0].message.content
    except Exception as e:
        reply = f"LLM 调用失败: {e}"

    # 检测并执行 ACTION
    action_result = ""
    if 'ACTION:' in reply or 'CREATE_DRAFT' in reply:
        action_result = execute_actions(reply)

    full_reply = reply
    if action_result:
        full_reply += f"\n\n---\n📋 执行结果:\n{action_result}"

    history[-1][1] = full_reply
    yield history, ""


def execute_actions(reply):
    """解析并执行 LLM 输出中的 ACTION"""
    results = []
    for line in reply.split('\n'):
        line = line.strip()
        if not line.startswith('ACTION:'):
            continue
        action = line[7:].strip()
        try:
            if action.startswith('CREATE_DRAFT'):
                r = api_post('create_draft', {'width': 1080, 'height': 1920})
                if r.get('success'):
                    session['draft_id'] = r['output']['draft_id']
                    results.append(f"✅ 草稿已创建: {session['draft_id']}")
                else:
                    results.append(f"❌ 创建失败: {r}")

            elif action.startswith('ADD_VIDEO') and session['draft_id']:
                asset = session['assets'][0] if session['assets'] else None
                if asset:
                    r = api_post('add_video', {
                        'draft_id': session['draft_id'],
                        'video_url': f'file:///{asset["path"]}',
                        'start': 0, 'end': 10,
                    })
                    results.append(f"{'✅' if r.get('success') else '❌'} 添加视频: {r.get('success', r.get('error', ''))}")

            elif action.startswith('ADD_TEXT') and session['draft_id']:
                # 解析参数
                import re
                text_match = re.search(r"text=([^,)]+)", action)
                text = text_match.group(1).strip('\"\'') if text_match else "文字"
                r = api_post('add_text', {
                    'draft_id': session['draft_id'],
                    'text': text,
                    'start': 0, 'end': 5,
                    'font_size': 12, 'font_color': '#FFFFFF',
                })
                results.append(f"{'✅' if r.get('success') else '❌'} 添加文字「{text}」")

            elif action.startswith('SAVE_DRAFT') and session['draft_id']:
                r = api_post('save_draft', {'draft_id': session['draft_id']})
                results.append(f"{'✅' if r.get('success') else '❌'} 保存草稿")

            elif action.startswith('RENDER') and session['draft_id']:
                r = api_post(f'render/draft/{session["draft_id"]}')
                if r.get('task_id'):
                    session['render_task'] = r['task_id']
                    results.append(f"✅ 渲染已提交: {r['task_id']}")
                else:
                    results.append(f"❌ 渲染失败: {r}")

            elif action.startswith('ANALYZE'):
                import re
                name_match = re.search(r"asset_name=([^)]+)", action)
                name = name_match.group(1).strip('\"\'') if name_match else ""
                asset = next((a for a in session['assets'] if name in a['name']), None)
                if asset and asset['type'] == 'video':
                    result = perceive_video(asset['path'], do_asr=False, frame_count=3)
                    asset['analysis'] = result
                    results.append(f"✅ 分析完成: {result.get('visual_analysis', '')[:100]}")
                else:
                    results.append(f"❌ 找不到视频资源: {name}")

        except Exception as e:
            results.append(f"❌ 执行出错: {e}")

    return '\n'.join(results)


# ============================================================ 渲染管理

def check_render():
    """检查渲染状态"""
    if not session.get('render_task'):
        return "未提交渲染", ""
    st = api_get(f'render/status/{session["render_task"]}')
    status = st.get('status', 'unknown')
    if status == 'done':
        mp4 = st.get('mp4_path', '')
        name = st.get('mp4_name', 'output.mp4')
        return f"✅ 渲染完成: {name}", mp4
    elif status == 'error':
        return f"❌ 渲染失败: {st.get('error', '')[:100]}", ""
    elif status == 'rendering':
        return "🔄 渲染中...", ""
    else:
        return f"⏳ {status}", ""


def render_current():
    """渲染当前草稿"""
    if not session.get('draft_id'):
        return "请先创建并保存草稿", ""
    r = api_post(f'render/draft/{session["draft_id"]}')
    if r.get('task_id'):
        session['render_task'] = r['task_id']
        return f"渲染已提交 (task: {r['task_id']})。点「刷新状态」查看进度。"
    return f"提交失败: {r}"


# ============================================================ UI

CSS = """
.gradio-container {max-width: 1200px !important; margin: auto;}
.asset-preview {overflow-y: auto; max-height: 300px;}
.chat-window {min-height: 400px;}
"""

with gr.Blocks(title="AI 视频编辑工作台") as app:
    gr.Markdown("# 🎬 AI 视频编辑工作台")
    gr.Markdown("导入资源 → 预览 → 跟 AI 对话 → 自动编辑 → 渲染")

    with gr.Row():
        # ===== 左侧：资源管理 =====
        with gr.Column(scale=1):
            gr.Markdown("### 📁 资源管理")

            upload = gr.File(
                label="上传素材",
                file_count="multiple",
                file_types=[".mp4", ".mov", ".avi", ".mkv", ".jpg", ".png", ".jpeg", ".mp3", ".wav"],
            )
            upload_status = gr.Textbox(label="状态", lines=3, interactive=False)

            asset_preview = gr.HTML(
                value="<p style='color:#888;text-align:center;padding:40px'>暂无资源</p>",
                label="资源预览",
                elem_classes=["asset-preview"],
            )

            with gr.Row():
                asset_selector = gr.Dropdown(
                    label="选择资源",
                    choices=[],
                    scale=3,
                )
                analyze_btn = gr.Button("🔍 分析", scale=1)

            analysis_result = gr.Textbox(label="分析结果", lines=12, interactive=False)

        # ===== 右侧：Agent 对话 + 渲染 =====
        with gr.Column(scale=2):
            gr.Markdown("### 🤖 AI 编辑助手")

            chatbot = gr.Chatbot(
                label="对话",
                height=400,
                elem_classes=["chat-window"],
            )

            with gr.Row():
                msg_input = gr.Textbox(
                    label="输入",
                    placeholder="描述你想做的视频，比如：用导入的素材做一个15秒的产品展示...",
                    scale=4,
                )
                send_btn = gr.Button("发送", scale=1, variant="primary")

            with gr.Row():
                msg_clear = gr.Button("清空对话")
                quick_create = gr.Button("📝 创建草稿")
                quick_render = gr.Button("🎬 渲染")
                refresh_btn = gr.Button("🔄 刷新状态")

            render_status = gr.Textbox(label="渲染状态", interactive=False)
            render_output = gr.File(label="下载成品", interactive=False)

    # ===== 事件绑定 =====
    upload.upload(on_upload, [upload], [upload_status, asset_preview, asset_selector])

    def analyze_wrapper(asset_name):
        gen = analyze_asset(asset_name)
        try:
            first = next(gen)  # "正在分析..."
            yield first[0], gr.update(), ""
        except StopIteration:
            return
        try:
            second = next(gen)  # 完成
            yield second[0], second[1], ""
        except StopIteration:
            return

    analyze_btn.click(analyze_wrapper, [asset_selector], [analysis_result, asset_preview, analysis_result])

    # 聊天
    msg_input.submit(chat_agent, [msg_input, chatbot], [chatbot, msg_input])
    send_btn.click(chat_agent, [msg_input, chatbot], [chatbot, msg_input])

    msg_clear.click(lambda: ([], ""), [chatbot], [msg_input])

    # 快捷操作
    def quick_create_draft():
        r = api_post('create_draft', {'width': 1080, 'height': 1920})
        if r.get('success'):
            session['draft_id'] = r['output']['draft_id']
            return f"草稿已创建: {session['draft_id']}"
        return f"失败: {r}"

    def quick_render_draft():
        return render_current()

    def refresh_status():
        status_text, mp4 = check_render()
        return status_text, mp4 if mp4 else None

    quick_create.click(quick_create_draft, [], [render_status])
    quick_render.click(quick_render_draft, [], [render_status])
    refresh_btn.click(refresh_status, [], [render_status, render_output])


if __name__ == "__main__":
    print("AI 视频编辑工作台: http://localhost:7860", flush=True)
    app.launch(server_name="127.0.0.1", server_port=7860, share=False, debug=False,
               css=CSS, theme=gr.themes.Soft(),
               prevent_thread_lock=False)
