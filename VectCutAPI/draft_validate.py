# draft_validate.py — 剪映草稿 JSON 完整性校验
#
# 剪映对坏草稿从不报错: 首页不显示卡片 / 打开弹"草稿丢失" / 导出拒弹窗,
# 全部表现成"点了没反应"。渲染前必须先自查, 把问题暴露在发起端。
# 校验项 = 历次事故的反向清单 (2026-08-24 共 7 案):
#   1. 根结构: duration>0, 有轨道, canvas/fps 存在
#   2. 轨道结构: 片段区间合法 (start>=0, duration>0, end 不超 duration 太多)
#   3. 素材引用: material_id / extra_material_refs 在素材表都有定义 (悬挂引用会让剪映拒收)
#   4. 素材可用性: 每个素材的文件可在 path / remote_url / 草稿 assets/ 内找到
#   5. 素材表自身: 无重复 id (重复会让部分剪映版本解析失败)
import json
import os
from typing import Any, Dict, List, Tuple


def _material_ids(mats: Dict[str, Any]) -> set:
    ids = set()
    for _key, mlist in mats.items():
        if not isinstance(mlist, list):
            continue
        for m in mlist:
            if not isinstance(m, dict):
                continue
            for f in ('id', 'material_id', 'global_id', 'animation_id',
                      'effect_id', 'fade_id', 'resource_id'):
                if m.get(f):
                    ids.add(m[f])
    return ids


def validate_draft_content(content: Dict[str, Any], draft_dir: str = None) -> Tuple[bool, List[str], List[str]]:
    """校验草稿 content dict. 返回 (ok, errors, warnings).
    draft_dir 给定时同时校验素材文件可用性 (assets/ 查找)."""
    errors: List[str] = []
    warnings: List[str] = []
    mats = content.get('materials') or {}
    tracks = content.get('tracks') or []
    duration = content.get('duration') or 0

    # ---- 1. 根结构 ----
    if not tracks:
        errors.append('草稿没有任何轨道 (tracks 为空)')
    if duration <= 0 and any(t.get('segments') for t in tracks):
        warnings.append('duration 为 0 但有片段内容 (时长记录可能损坏)')

    # ---- 2. 轨道结构 ----
    total_segs = 0
    for t in tracks:
        tname = t.get('name') or t.get('type') or '?'
        for i, s in enumerate(t.get('segments') or []):
            total_segs += 1
            tr = s.get('target_timerange') or {}
            st, dur = tr.get('start', 0), tr.get('duration', 0)
            if st < 0:
                errors.append(f'轨道 {tname} 第{i}段 start<0 ({st})')
            if dur <= 0:
                errors.append(f'轨道 {tname} 第{i}段 duration<=0 ({dur}) — 零时长段会被剪映静默丢弃')
            elif duration > 0 and st + dur > duration + 1_000_000:
                warnings.append(f'轨道 {tname} 第{i}段 end({(st + dur) / 1e6:.1f}s) 超出草稿 duration({duration / 1e6:.1f}s)')
    if tracks and total_segs == 0:
        errors.append('所有轨道都没有片段 (空时间线, 剪映会拒导出)')

    # ---- 3. 素材引用完整性 ----
    ids = _material_ids(mats)
    dangling = 0
    for t in tracks:
        tname = t.get('name') or '?'
        for i, s in enumerate(t.get('segments') or []):
            mid = s.get('material_id')
            if mid and mid not in ids:
                errors.append(f'轨道 {tname} 第{i}段 material_id 悬挂 ({mid[:12]}…)')
                dangling += 1
            for ref in (s.get('extra_material_refs') or []):
                if ref not in ids:
                    errors.append(f'轨道 {tname} 第{i}段 extra_material_refs 悬挂 ({ref[:12]}…)')
                    dangling += 1
    if dangling > 5:
        errors.append(f'共 {dangling} 个悬挂引用 — 剪映会拒收此草稿 (首页不显示卡片)')

    # ---- 4. 素材可用性 ----
    assets_index = set()
    if draft_dir and os.path.isdir(draft_dir):
        for root, _dirs, files in os.walk(os.path.join(draft_dir, 'assets')):
            for f in files:
                assets_index.add(f)
    missing_files = []
    for mkey in ('videos', 'audios'):
        for m in (mats.get(mkey) or []):
            if not isinstance(m, dict):
                continue
            name = m.get('material_name') or m.get('name') or '?'
            path = (m.get('path') or '').replace('\\', '/')
            remote = (m.get('remote_url') or '').replace('\\', '/')
            if path and os.path.isfile(path):
                continue
            if remote and os.path.isfile(remote):
                continue
            if name in assets_index:
                continue
            missing_files.append(name)
    for name in missing_files[:8]:
        errors.append(f'素材文件不可用: {name} (path/remote_url/assets 都找不到)')
    if len(missing_files) > 8:
        errors.append(f'…以及另外 {len(missing_files) - 8} 个素材文件不可用')
    if missing_files:
        errors.append('存在素材缺失 — 剪映会弹"草稿丢失"横幅并吞掉自动化点击')

    # ---- 5. 素材表重复 id ----
    seen = {}
    for _key, mlist in mats.items():
        if not isinstance(mlist, list):
            continue
        for m in mlist:
            if not isinstance(m, dict):
                continue
            mid = m.get('id') or m.get('material_id') or m.get('global_id')
            if not mid:
                continue
            seen[mid] = seen.get(mid, 0) + 1
    dup = [k for k, n in seen.items() if n > 1]
    if dup:
        warnings.append(f'素材表存在重复 id x{len(dup)} (如 {dup[0][:12]}…) — 部分剪映版本会解析失败')

    return (len(errors) == 0, errors, warnings)


def validate_draft_dir(draft_dir: str) -> Tuple[bool, List[str], List[str]]:
    """校验磁盘上的草稿目录. 返回 (ok, errors, warnings)."""
    for name in ('draft_content.json', 'draft_info.json'):
        cp = os.path.join(draft_dir, name)
        if os.path.isfile(cp):
            try:
                content = json.load(open(cp, encoding='utf-8'))
            except Exception as e:
                return False, [f'{name} 不是合法 JSON: {e}'], []
            return validate_draft_content(content, draft_dir=draft_dir)
    return False, [f'草稿目录缺少 draft_content.json: {draft_dir}'], []


if __name__ == '__main__':
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else '.'
    ok, errors, warnings = validate_draft_dir(target)
    for w in warnings:
        print(f'⚠️  {w}')
    for e in errors:
        print(f'❌ {e}')
    print('✅ 草稿校验通过' if ok else f'❌ 校验失败 ({len(errors)} 个错误)')
    sys.exit(0 if ok else 1)
