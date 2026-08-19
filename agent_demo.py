# agent_demo.py — 端到端 AI 视频编辑 Agent Demo
# 不依赖 Pi/外部框架，用 Python 直连 LLM + render_server + perceive
# 演示完整闭环: 用户意图 → LLM规划 → 感知素材 → 组装草稿 → 渲染 → 质检
#
# 用法:
#   python agent_demo.py "做一个关于海边日落的15秒短视频，配文字'美好的一天'"
#
# 需要:
#   1. render_server 在 localhost:9010 运行
#   2. 阿里云 DashScope API key (Qwen3.7-Plus)

import os, sys, json, time

try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

from openai import OpenAI
from perceive import perceive_video, QWEN_API_KEY, QWEN_BASE_URL
import config
from cli.client import get_client

LLM_MODEL = "qwen3.7-plus"

llm = OpenAI(api_key=QWEN_API_KEY, base_url=QWEN_BASE_URL)
_client = get_client()


def llm_chat(system, user, max_tokens=2000):
    """调 LLM，返回文本"""
    r = llm.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens
    )
    return r.choices[0].message.content


def api_post(endpoint, data=None):
    """调 render_server (复用 cli.client)"""
    return _client.post(endpoint, json=data)


def api_get(endpoint):
    return _client.get(endpoint)


def plan_video(user_intent, asset_analyses):
    """让 LLM 基于用户意图 + 素材感知，规划视频编辑方案"""
    system = """你是一个专业的短视频导演。根据用户的创意意图和可用素材，规划一个视频编辑方案。

返回 JSON 格式:
{
  "title": "视频标题",
  "canvas": {"width": 1080, "height": 1920},
  "scenes": [
    {
      "type": "video",
      "source_start": 0,
      "duration": 5,
      "transition": "fade_in",
      "note": "这个场景用什么素材的哪段"
    },
    {
      "type": "text",
      "content": "要显示的文字",
      "start": 0,
      "duration": 3,
      "style": {"font_size": 12, "font_color": "#FFFFFF"},
      "position": {"x": 0, "y": 0.5}
    }
  ],
  "narration": "旁白/字幕内容"
}

规则:
- 总时长 10-30 秒
- 文字要简洁有力
- 合理使用素材（根据素材分析结果选择精彩片段）
- 位置 y: 1=顶部, 0=中间, -1=底部; x: -1=左, 0=中, 1=右"""

    asset_desc = json.dumps(asset_analyses, ensure_ascii=False, indent=2)
    plan = llm_chat(system, f"用户意图: {user_intent}\n\n可用素材:\n{asset_desc}")

    # 提取 JSON
    try:
        start = plan.find('{')
        end = plan.rfind('}') + 1
        return json.loads(plan[start:end])
    except:
        return {"error": "LLM 返回解析失败", "raw": plan}


def execute_plan(plan, assets):
    """执行编辑方案: 创建草稿 → 加视频 → 加文字 → 保存"""
    # 创建草稿
    canvas = plan.get('canvas', {'width': 1080, 'height': 1920})
    r = api_post('create_draft', canvas)
    if not r.get('success'):
        raise Exception(f"创建草稿失败: {r}")
    draft_id = r['output']['draft_id']
    print(f"  草稿: {draft_id}")

    cursor = 0
    for i, scene in enumerate(plan.get('scenes', [])):
        stype = scene.get('type', 'text')
        print(f"  [{i+1}] {stype}: {scene.get('content', scene.get('note', ''))[:40]}")

        if stype == 'video':
            # 用第一个可用素材
            asset = assets[0] if assets else None
            if asset:
                data = {
                    'draft_id': draft_id,
                    'video_url': asset.get('url', ''),
                    'start': scene.get('source_start', 0),
                    'target_start': scene.get('target_start', cursor),
                }
                dur = scene.get('duration', 5)
                data['end'] = data['start'] + dur
                if scene.get('transition'):
                    data['transition'] = scene['transition']
                api_post('add_video', data)
                cursor += dur

        elif stype == 'text':
            data = {
                'draft_id': draft_id,
                'text': scene.get('content', ''),
                'start': scene.get('start', cursor),
                'end': scene.get('start', cursor) + scene.get('duration', 3),
                'track_name': f'text_{i}',
            }
            style = scene.get('style', {})
            if style.get('font_size'): data['font_size'] = style['font_size']
            if style.get('font_color'): data['font_color'] = style['font_color']

            pos = scene.get('position', {})
            if pos.get('x') is not None: data['transform_x'] = pos['x']
            if pos.get('y') is not None: data['transform_y'] = pos['y']

            api_post('add_text', data)

    # 保存
    r = api_post('save_draft', {'draft_id': draft_id})
    print(f"  保存: {r.get('success', False)}")
    return draft_id


def main():
    user_intent = ' '.join(sys.argv[1:]) if len(sys.argv) > 1 else "做一个15秒的海边日落视频"

    print(f"\n{'='*60}")
    print(f"  AI 视频编辑 Agent")
    print(f"{'='*60}")
    print(f"\n用户意图: {user_intent}")

    # === 步骤1: 感知素材 ===
    print(f"\n[1/5] 感知素材...")
    VIDEOS = config.VIDEOS_DIR
    mp4s = sorted([f for f in os.listdir(VIDEOS) if f.endswith('.mp4')],
                  key=lambda f: os.path.getmtime(os.path.join(VIDEOS, f)), reverse=True)

    assets = []
    if mp4s:
        video_path = os.path.join(VIDEOS, mp4s[0])
        print(f"  分析: {mp4s[0]}")
        analysis = perceive_video(video_path, do_asr=False, frame_count=4)
        assets.append({
            'url': f'file:///{video_path}',
            'path': video_path,
            'analysis': analysis.get('visual_analysis', ''),
            'duration': analysis.get('meta', {}).get('duration', 5),
            'filename': mp4s[0],
        })
        print(f"  内容: {analysis.get('visual_analysis', '')[:100]}...")

    # === 步骤2: LLM 规划 ===
    print(f"\n[2/5] AI 规划编辑方案...")
    plan = plan_video(user_intent, assets)
    if 'error' in plan:
        print(f"  规划失败: {plan}")
        return
    print(f"  标题: {plan.get('title', '未命名')}")
    print(f"  场景: {len(plan.get('scenes', []))} 个")

    # === 步骤3: 组装草稿 ===
    print(f"\n[3/5] 组装草稿...")
    draft_id = execute_plan(plan, assets)

    # === 步骤4: 渲染 ===
    print(f"\n[4/5] 提交渲染...")
    r = api_post(f'render/draft/{draft_id}')
    task_id = r.get('task_id')
    if not task_id:
        print(f"  渲染提交失败: {r}")
        return
    print(f"  任务: {task_id}")

    # 等渲染完成
    print(f"  等待完成", end='', flush=True)
    for _ in range(120):
        time.sleep(5)
        print('.', end='', flush=True)
        st = api_get(f'render/status/{task_id}')
        if st.get('status') == 'done':
            mp4 = st.get('mp4_path', '')
            print(f"\n  完成: {st.get('mp4_name', '')}")
            break
        elif st.get('status') == 'error':
            print(f"\n  渲染失败: {st.get('error', '')[:100]}")
            return
    else:
        print("\n  渲染超时")
        return

    # === 步骤5: 质检 ===
    print(f"\n[5/5] AI 质检...")
    if mp4 and os.path.exists(mp4):
        from perceive import perceive_result
        expectations = f"标题: {plan.get('title','')}, 场景数: {len(plan.get('scenes',[]))}"
        check = perceive_result(mp4, expectations)
        print(f"  质检: {check.get('quality', '')[:200]}")

    print(f"\n{'='*60}")
    print(f"  完成! 输出: {mp4}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
