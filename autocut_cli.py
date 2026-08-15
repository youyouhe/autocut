# autocut_cli.py — AutoCut 命令行入口
# 供脚本/其他调用者使用, 通过 render_server REST API 操作.
# 用法: python autocut_cli.py <command> [--json]
import sys
import argparse

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from cli.client import ApiClient, ApiError
from cli import output


def _client(args):
    return ApiClient(base_url=getattr(args, 'api', None))


# ============================================================ 命令实现

def cmd_health(args):
    output.print_result(_client(args).health(), args.json)


def cmd_perceive(args):
    c = _client(args)
    r = c.perceive(args.path, do_asr=not args.no_asr, frames=args.frames)
    output.print_result(r, args.json)


def cmd_draft_create(args):
    c = _client(args)
    r = c.create_draft(width=args.width, height=args.height)
    output.print_result(r, args.json)
    if r.get('output', {}).get('draft_id') and not args.json:
        print(f"draft_id: {r['output']['draft_id']}")


def cmd_draft_add_video(args):
    c = _client(args)
    kw = {}
    if args.start is not None: kw['start'] = args.start
    if args.end is not None: kw['end'] = args.end
    if args.volume is not None: kw['volume'] = args.volume
    if args.transition: kw['transition'] = args.transition
    r = c.add_video(args.draft_id, args.url, **kw)
    output.print_result(r, args.json)


def cmd_draft_add_text(args):
    c = _client(args)
    kw = {'start': args.start, 'end': args.end}
    if args.size is not None: kw['font_size'] = args.size
    if args.color: kw['font_color'] = args.color
    r = c.add_text(args.draft_id, args.text, **kw)
    output.print_result(r, args.json)


def cmd_draft_add_audio(args):
    c = _client(args)
    kw = {}
    if args.volume is not None: kw['volume'] = args.volume
    if args.start is not None: kw['start'] = args.start
    r = c.add_audio(args.draft_id, args.url, **kw)
    output.print_result(r, args.json)


def cmd_draft_save(args):
    c = _client(args)
    r = c.save_draft(args.draft_id)
    output.print_result(r, args.json)


def cmd_draft_list(args):
    output.print_result(_client(args).list_drafts(), args.json)


def cmd_render(args):
    c = _client(args)
    # 渲染前先保存草稿
    if args.draft_id:
        c.save_draft(args.draft_id)
        r = c.render(args.draft_id)
    else:
        r = c.render_zip(args.zip, args.draft_name)
    if not r.get('task_id'):
        output.print_error(r.get('error', 'no task_id returned'), args.json)
    task_id = r['task_id']
    if args.wait:
        def _progress(st):
            if args.progress and st.get('progress'):
                p = st['progress']
                stage = p.get('stage', '')
                mb = f" {p['temp_bytes']/1048576:.1f}MB" if p.get('temp_bytes') else ''
                sys.stderr.write(f"\r  [{stage}]{mb} " + ' ' * 10)
                sys.stderr.flush()
        try:
            final = c.render_wait(task_id, timeout=args.timeout, poll=2.0,
                                  on_progress=_progress if args.progress else None)
        except ApiError as e:
            output.print_error(str(e), args.json)
        if args.progress:
            sys.stderr.write('\n')
        if final.get('status') != 'done':
            output.print_error(final.get('error', f"render failed: {final.get('status')}"),
                               args.json)
        if args.output:
            c.render_download(task_id, args.output)
            if args.json:
                print(__import__('json').dumps(
                    {'task_id': task_id, 'status': 'done', 'saved': args.output},
                    ensure_ascii=False))
            else:
                print(f"saved: {args.output}")
        else:
            output.print_result(final, args.json)
    else:
        output.print_result(r, args.json)
        if not args.json:
            print(f"poll: /render/status/{task_id}")


def cmd_render_status(args):
    output.print_result(_client(args).render_status(args.task_id), args.json)


def cmd_render_list(args):
    output.print_result(_client(args).render_list(), args.json)


def cmd_render_download(args):
    c = _client(args)
    dest = args.output or f"{args.task_id}.mp4"
    c.render_download(args.task_id, dest)
    if args.json:
        print(__import__('json').dumps({'task_id': args.task_id, 'saved': dest},
                                       ensure_ascii=False))
    else:
        print(f"saved: {dest}")


def cmd_template_list(args):
    output.print_result(_client(args).list_templates(), args.json)


def cmd_template_render(args):
    import json as _json
    variables = _json.loads(args.vars) if args.vars else {}
    r = _client(args).render_template(args.template, variables, do_render=args.render)
    output.print_result(r, args.json)


def cmd_localsend_start(args):
    output.print_result(_client(args).localsend_start(), args.json)


def cmd_localsend_stop(args):
    output.print_result(_client(args).localsend_stop(), args.json)


def cmd_localsend_status(args):
    output.print_result(_client(args).localsend_status(), args.json)


# ============================================================ argparse 组装

def _add_common(p, suppress=False):
    """全局参数 --api/--json. 子命令用 suppress=True 避免覆盖主解析器的值."""
    d = argparse.SUPPRESS if suppress else None
    p.add_argument('--api', default=d, help='render_server 地址 (默认 $AUTOCUT_API 或 http://127.0.0.1:9002)')
    p.add_argument('--json', action='store_true', default=d, help='输出纯 JSON (脚本消费)')


def build_parser():
    p = argparse.ArgumentParser(prog='autocut', description='AutoCut CLI — 剪映自动化视频生产')
    _add_common(p)
    sub = p.add_subparsers(dest='command', required=True)

    # health
    sp = sub.add_parser('health', help='健康检查')
    _add_common(sp, suppress=True)
    sp.set_defaults(func=cmd_health)

    # perceive
    sp = sub.add_parser('perceive', help='分析视频 (画面+语音+场景)')
    _add_common(sp, suppress=True)
    sp.add_argument('path')
    sp.add_argument('--no-asr', action='store_true', help='跳过语音转录')
    sp.add_argument('--frames', type=int, default=4, help='抽帧数')
    sp.set_defaults(func=cmd_perceive)

    # draft
    sp = sub.add_parser('draft', help='草稿管理')
    _add_common(sp, suppress=True)
    dsub = sp.add_subparsers(dest='draft_cmd', required=True)

    d = dsub.add_parser('create', help='创建草稿')
    _add_common(d, suppress=True)
    d.add_argument('--width', type=int, default=1080)
    d.add_argument('--height', type=int, default=1920)
    d.set_defaults(func=cmd_draft_create)

    d = dsub.add_parser('add-video', help='添加视频片段')
    _add_common(d, suppress=True)
    d.add_argument('draft_id'); d.add_argument('url')
    d.add_argument('--start', type=float); d.add_argument('--end', type=float)
    d.add_argument('--volume', type=float); d.add_argument('--transition')
    d.set_defaults(func=cmd_draft_add_video)

    d = dsub.add_parser('add-text', help='添加文字')
    _add_common(d, suppress=True)
    d.add_argument('draft_id'); d.add_argument('text')
    d.add_argument('--start', type=float, default=0); d.add_argument('--end', type=float, default=5)
    d.add_argument('--size', type=float); d.add_argument('--color')
    d.set_defaults(func=cmd_draft_add_text)

    d = dsub.add_parser('add-audio', help='添加音频')
    _add_common(d, suppress=True)
    d.add_argument('draft_id'); d.add_argument('url')
    d.add_argument('--volume', type=float); d.add_argument('--start', type=float)
    d.set_defaults(func=cmd_draft_add_audio)

    d = dsub.add_parser('save', help='保存草稿')
    _add_common(d, suppress=True)
    d.add_argument('draft_id')
    d.set_defaults(func=cmd_draft_save)

    d = dsub.add_parser('list', help='列出剪映草稿')
    _add_common(d, suppress=True)
    d.set_defaults(func=cmd_draft_list)

    # render
    sp = sub.add_parser('render', help='渲染草稿为 mp4')
    _add_common(sp, suppress=True)
    sp.add_argument('draft_id', nargs='?', help='草稿ID或文件夹名 (与 --zip 二选一)')
    sp.add_argument('--zip', help='上传 zip 草稿渲染')
    sp.add_argument('--draft-name', help='zip 渲染时的草稿名')
    sp.add_argument('--wait', action='store_true', help='阻塞等待渲染完成')
    sp.add_argument('--progress', action='store_true', help='(配合 --wait) 打印进度到 stderr')
    sp.add_argument('--timeout', type=int, default=600, help='--wait 超时秒数')
    sp.add_argument('-o', '--output', help='渲染完成后下载 mp4 到该路径')
    sp.set_defaults(func=cmd_render)

    sp = sub.add_parser('render-status', help='查询渲染任务状态')
    _add_common(sp, suppress=True)
    sp.add_argument('task_id')
    sp.set_defaults(func=cmd_render_status)

    sp = sub.add_parser('render-list', help='渲染任务列表')
    _add_common(sp, suppress=True)
    sp.set_defaults(func=cmd_render_list)

    sp = sub.add_parser('render-download', help='下载渲染结果 mp4')
    _add_common(sp, suppress=True)
    sp.add_argument('task_id')
    sp.add_argument('-o', '--output')
    sp.set_defaults(func=cmd_render_download)

    # template
    sp = sub.add_parser('template', help='模板管理')
    _add_common(sp, suppress=True)
    tsub = sp.add_subparsers(dest='tpl_cmd', required=True)
    t = tsub.add_parser('list', help='列出模板')
    _add_common(t, suppress=True); t.set_defaults(func=cmd_template_list)
    t = tsub.add_parser('render', help='执行模板')
    _add_common(t, suppress=True)
    t.add_argument('template'); t.add_argument('--vars', help='变量 JSON 字符串')
    t.add_argument('--render', action='store_true', help='组装后自动渲染')
    t.set_defaults(func=cmd_template_render)

    # localsend
    sp = sub.add_parser('localsend', help='LocalSend 接收端')
    _add_common(sp, suppress=True)
    lsub = sp.add_subparsers(dest='ls_cmd', required=True)
    t = lsub.add_parser('start', help='启动接收')
    _add_common(t, suppress=True); t.set_defaults(func=cmd_localsend_start)
    t = lsub.add_parser('stop', help='停止接收')
    _add_common(t, suppress=True); t.set_defaults(func=cmd_localsend_stop)
    t = lsub.add_parser('status', help='接收状态')
    _add_common(t, suppress=True); t.set_defaults(func=cmd_localsend_status)

    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    try:
        args.func(args)
    except ApiError as e:
        output.print_error(str(e), args.json)
    except Exception as e:
        output.print_error(str(e), args.json)


if __name__ == '__main__':
    main()
