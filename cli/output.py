# cli/output.py — 输出格式化: 人类可读 (默认) vs 纯 JSON (--json)
import json
import sys


def print_result(data, as_json=False):
    """打印结果. as_json=True 输出纯 JSON (脚本消费); 否则简单人类可读."""
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if data is None:
        return
    if isinstance(data, dict):
        for k, v in data.items():
            print(f"{k}: {_fmt(v)}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                print(json.dumps(item, ensure_ascii=False))
            else:
                print(item)
    else:
        print(data)


def _fmt(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def print_error(msg, as_json=False, exit_code=1):
    if as_json:
        print(json.dumps({'error': str(msg)}, ensure_ascii=False))
    else:
        print(f"error: {msg}", file=sys.stderr)
    sys.exit(exit_code)
