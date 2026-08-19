# template_engine.py — 声明式视频模板引擎
# YAML 模板 + 变量填充 → VectCutAPI 自动组装草稿 → render_server 渲染
#
# 模板格式:
#   name: 模板名称
#   canvas: {width, height}
#   scenes:
#     - type: video / text / audio / subtitle
#       ... (具体参数)
#   render: {resolution, framerate}
#
# 用法:
#   from template_engine import render_template
#   render_template("templates/product_intro.yaml", {
#       "product_name": "AI助手",
#       "demo_video": "https://example.com/demo.mp4",
#   })

import os, sys, json, time
try:
    import yaml
except ImportError:
    print("需要 pyyaml: pip install pyyaml"); sys.exit(1)

try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config
from cli.client import get_client

TEMPLATES_DIR = config.TEMPLATES_DIR

# ============================================================ 工具函数

def _api(endpoint, data=None, method='POST'):
    """调 render_server REST API (复用 cli.client)."""
    c = get_client()
    if method == 'POST':
        return c.post(endpoint, json=data)
    return c.get(endpoint)


def _fill(template_str, variables):
    """递归填充 {{var}} 占位符"""
    if isinstance(template_str, str):
        for k, v in variables.items():
            template_str = template_str.replace(f"{{{{{k}}}}}", str(v))
        return template_str
    elif isinstance(template_str, dict):
        return {k: _fill(v, variables) for k, v in template_str.items()}
    elif isinstance(template_str, list):
        return [_fill(item, variables) for item in template_str]
    return template_str


def _parse_time(t, default=0):
    """解析时间字符串: '3s' → 3, '1.5s' → 1.5, 10 → 10"""
    if t is None:
        return default
    if isinstance(t, (int, float)):
        return t
    s = str(t).strip().rstrip('s')
    try:
        return float(s)
    except ValueError:
        return default


# ============================================================ Scene 处理器

def _scene_video(scene, ctx):
    """处理视频场景"""
    data = {
        'draft_id': ctx['draft_id'],
        'video_url': scene.get('source', scene.get('url', '')),
        'start': _parse_time(scene.get('source_start', 0)),
        'target_start': _parse_time(scene.get('target_start', ctx['cursor'])),
    }
    if scene.get('source_end'):
        data['end'] = _parse_time(scene['source_end'])
    if scene.get('duration'):
        data['end'] = _parse_time(scene.get('source_start', 0)) + _parse_time(scene['duration'])
    if scene.get('volume') is not None:
        data['volume'] = scene['volume']
    if scene.get('transition'):
        data['transition'] = scene['transition']
    if scene.get('scale_x') is not None:
        data['scale_x'] = scene['scale_x']
    if scene.get('scale_y') is not None:
        data['scale_y'] = scene['scale_y']
    if scene.get('transform_y') is not None:
        data['transform_y'] = scene['transform_y']

    result = _api('add_video', data)
    # 叠加层不推进 cursor
    if scene.get('overlay'):
        return result
    # 更新时间游标
    dur = _parse_time(scene.get('duration'), _parse_time(scene.get('source_end', 0)) - _parse_time(scene.get('source_start', 0)))
    if dur > 0:
        ctx['cursor'] = _parse_time(scene.get('target_start', ctx['cursor'])) + dur
    return result


def _scene_text(scene, ctx):
    """处理文字场景"""
    data = {
        'draft_id': ctx['draft_id'],
        'text': scene.get('content', scene.get('text', '')),
        'start': _parse_time(scene.get('start', ctx['cursor'])),
        'end': _parse_time(scene.get('end', ctx['cursor'] + _parse_time(scene.get('duration', 5)))),
    }
    # 样式
    style = scene.get('style', {})
    if style.get('font'): data['font'] = style['font']
    if style.get('font_size'): data['font_size'] = style['font_size']
    if style.get('font_color'): data['font_color'] = style['font_color']
    if style.get('shadow_enabled') is not None: data['shadow_enabled'] = style['shadow_enabled']
    if style.get('background_color'): data['background_color'] = style['background_color']
    if style.get('background_alpha') is not None: data['background_alpha'] = style['background_alpha']

    # 位置
    pos = scene.get('position', {})
    if pos.get('x') is not None: data['transform_x'] = pos['x']
    if pos.get('y') is not None: data['transform_y'] = pos['y']

    # 动画
    anim = scene.get('animation', {})
    if anim.get('intro'): data['intro_animation'] = anim['intro']
    if anim.get('outro'): data['outro_animation'] = anim['outro']

    # 轨道名
    if scene.get('track_name'):
        data['track_name'] = scene['track_name']

    result = _api('add_text', data)
    # 叠加层不推进 cursor
    if scene.get('overlay'):
        return result
    ctx['cursor'] = data['end']
    return result


def _scene_image(scene, ctx):
    """处理图片场景（作为背景层或叠加层）. add_image 接口的 start/end 直接是时间轴位置
    (图片没有"源片段", 不像视频要分 source_start/target_start)."""
    start = _parse_time(scene.get('target_start', ctx['cursor']))
    dur = _parse_time(scene.get('duration', 5))
    data = {
        'draft_id': ctx['draft_id'],
        'image_url': scene.get('source', scene.get('url', '')),
        'start': start,
        'end': start + dur,
    }
    # 图片特有
    if scene.get('intro_animation'): data['intro_animation'] = scene['intro_animation']
    if scene.get('outro_animation'): data['outro_animation'] = scene['outro_animation']
    if scene.get('scale_x') is not None: data['scale_x'] = scene['scale_x']
    if scene.get('scale_y') is not None: data['scale_y'] = scene['scale_y']
    if scene.get('transform_y') is not None: data['transform_y'] = scene['transform_y']
    if scene.get('transition'): data['transition'] = scene['transition']
    if scene.get('track_name'): data['track_name'] = scene['track_name']

    result = _api('add_image', data)
    # 叠加层不推进 cursor
    if scene.get('overlay'):
        return result
    ctx['cursor'] = start + dur
    return result


def _scene_audio(scene, ctx):
    """处理音频场景"""
    data = {
        'draft_id': ctx['draft_id'],
        'audio_url': scene.get('source', scene.get('url', '')),
        'start': _parse_time(scene.get('start', 0)),
        'volume': scene.get('volume', 0.5),
    }
    if scene.get('end'):
        data['end'] = _parse_time(scene['end'])
    return _api('add_audio', data)


def _scene_subtitle(scene, ctx):
    """处理字幕场景（从 SRT 或文字列表）"""
    items = scene.get('items', [])
    track_name = scene.get('track_name', 'subtitle')
    style = scene.get('style', {})

    results = []
    for item in items:
        data = {
            'draft_id': ctx['draft_id'],
            'text': item.get('text', ''),
            'start': _parse_time(item.get('start', 0)),
            'end': _parse_time(item.get('end', item.get('start', 0)) + 3),
            'track_name': track_name,
        }
        if style.get('font_size'): data['font_size'] = style['font_size']
        if style.get('font_color'): data['font_color'] = style['font_color']
        results.append(_api('add_text', data))
    return results


SCENE_HANDLERS = {
    'video': _scene_video,
    'image': _scene_image,
    'text': _scene_text,
    'audio': _scene_audio,
    'subtitle': _scene_subtitle,
}


# ============================================================ 核心引擎

def render_template(template_path, variables=None, do_render=False, draft_folder=None):
    """
    执行模板: 读取 YAML → 填充变量 → 组装草稿 → (可选)渲染

    Args:
        template_path: YAML 模板路径
        variables: 变量字典 {{var}} → value
        do_render: 是否自动渲染
        draft_folder: 草稿输出目录

    Returns:
        {draft_id, draft_url, render_task_id?}
    """
    variables = variables or {}

    # ① 加载模板
    with open(template_path, encoding='utf-8') as f:
        tpl = yaml.safe_load(f)

    # ② 填充变量
    tpl = _fill(tpl, variables)

    print(f"[模板] {tpl.get('name', '未命名')}", flush=True)

    # ③ 创建草稿
    canvas = tpl.get('canvas', {})
    r = _api('create_draft', {
        'width': canvas.get('width', 1080),
        'height': canvas.get('height', 1920),
    })
    if not r.get('success'):
        raise Exception(f"创建草稿失败: {r}")
    draft_id = r['output']['draft_id']
    print(f"[草稿] draft_id={draft_id}", flush=True)

    # ④ 处理场景
    ctx = {'draft_id': draft_id, 'cursor': 0.0}
    scenes = tpl.get('scenes', [])

    for i, scene in enumerate(scenes):
        scene_type = scene.get('type', 'text')
        handler = SCENE_HANDLERS.get(scene_type)
        if not handler:
            print(f"  [跳过] 未知场景类型: {scene_type}", flush=True)
            continue

        desc = scene.get('description', scene.get('content', scene.get('source', '')))
        print(f"  [{i+1}/{len(scenes)}] {scene_type}: {str(desc)[:50]}", flush=True)

        try:
            handler(scene, ctx)
        except Exception as e:
            print(f"    ⚠ 失败: {e}", flush=True)

    # ⑤ 保存草稿
    save_data = {'draft_id': draft_id}
    if draft_folder:
        save_data['draft_folder'] = draft_folder
    r = _api('save_draft', save_data)
    print(f"[保存] {r.get('success', False)}", flush=True)

    result = {'draft_id': draft_id, 'save': r}

    # ⑥ 渲染（可选）
    if do_render:
        print("[渲染] 提交...", flush=True)
        r = _api(f'render/draft/{draft_id}')
        result['render'] = r
        if r.get('task_id'):
            print(f"[渲染] task_id={r['task_id']} (轮询 /render/status/{r['task_id']})", flush=True)

    return result


import re

_VAR_RE = re.compile(r'\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}')


def _extract_variables(tpl, raw_text):
    """变量名来源: 优先用顶层 variables: 字典声明; 若未声明, 从原始 YAML 文本里
    正则提取所有 {{xxx}} 占位符 (按首次出现顺序去重) — 现有 3 个模板均未声明
    variables:, 只在 scenes 里散落使用占位符, 否则前端拿不到字段渲染表单."""
    declared = tpl.get('variables')
    if isinstance(declared, dict) and declared:
        return list(declared.keys())
    seen = []
    for name in _VAR_RE.findall(raw_text):
        if name not in seen:
            seen.append(name)
    return seen


def list_templates():
    """列出所有可用模板"""
    templates = []
    if not os.path.isdir(TEMPLATES_DIR):
        return templates
    for f in sorted(os.listdir(TEMPLATES_DIR)):
        if f.endswith('.yaml') or f.endswith('.yml'):
            path = os.path.join(TEMPLATES_DIR, f)
            try:
                with open(path, encoding='utf-8') as fh:
                    raw_text = fh.read()
                tpl = yaml.safe_load(raw_text)
                templates.append({
                    'file': f,
                    'name': tpl.get('name', f),
                    'description': tpl.get('description', ''),
                    'variables': _extract_variables(tpl, raw_text),
                })
            except Exception:
                pass
    return templates


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='视频模板引擎')
    ap.add_argument('command', choices=['list', 'render'], help='list=列出模板, render=执行模板')
    ap.add_argument('--template', '-t', help='模板文件路径')
    ap.add_argument('--vars', '-v', help='变量JSON字符串', default='{}')
    ap.add_argument('--render', '-r', action='store_true', help='组装后自动渲染')
    args = ap.parse_args()

    if args.command == 'list':
        templates = list_templates()
        if not templates:
            print(f'没有模板。把 .yaml 放到 {TEMPLATES_DIR}/')
        for t in templates:
            print(f"  {t['file']}: {t['name']} ({t['description']})")
            if t['variables']:
                print(f"    变量: {t['variables']}")

    elif args.command == 'render':
        if not args.template:
            print('需要 --template'); sys.exit(1)
        variables = json.loads(args.vars)
        result = render_template(args.template, variables, do_render=args.render)
        print(json.dumps(result, ensure_ascii=False, indent=2))
