"""草稿复杂编辑能力实现 (Phase 3/4): 修改/替换/滤镜/转场/动画/拆分/复制/搬移片段

给 Agent 提供"编辑已存在内容"的能力 (create/add 之外的另一半)。
本模块直接操作 DRAFT_CACHE 中存活的 Script_file 对象 (模式仿 delete_impl.py):
改完调用方负责 save_draft 落盘。

时间单位: 库内部微秒, 本模块 API 秒。定位方式统一: segment_id 或 track_name+index。
"""
import copy
import os
import logging
from typing import Any, Dict, List, Optional, Tuple

from draft_cache import DRAFT_CACHE

logger = logging.getLogger(__name__)

SEC = 1_000_000  # 一秒 = 1e6 微秒


def _get_script(draft_id: str):
    """取存活的 Script_file; 缓存 miss 时从磁盘 load_template 载入 (冷草稿自愈).
    与 render_server._warmup_draft 同逻辑 — 编辑类路由不能依赖调用方先 warmup,
    否则服务重启后所有编辑操作报"不存在于缓存"; 且上游 get_or_create_draft 类函数
    在缓存 miss 时会静默新建空草稿, 让后续操作全部打在空壳上."""
    if draft_id in DRAFT_CACHE:
        return DRAFT_CACHE[draft_id]
    import os as _os
    import pyJianYingDraft as _draft
    from draft_cache import update_cache
    here = _os.path.dirname(_os.path.abspath(__file__))
    for name in ('draft_content.json', 'draft_info.json'):
        cp = _os.path.join(here, draft_id, name)
        if _os.path.isfile(cp):
            script = _draft.Script_file.load_template(cp)
            update_cache(draft_id, script)
            logger.info(f"冷草稿已从磁盘载入缓存: {draft_id} ({name})")
            return script
    raise KeyError(f"草稿 '{draft_id}' 不存在于缓存且磁盘上未找到 (可能未创建或已过期)")


def _all_tracks(script) -> List[Tuple[str, Any]]:
    """[(track_name, track), ...], 普通轨道 + 导入轨道."""
    pairs = [(name, t) for name, t in script.tracks.items()]
    for t in getattr(script, "imported_tracks", []) or []:
        pairs.append((t.name, t))
    return pairs


def locate_segment(script, segment_id: Optional[str] = None,
                   track_name: Optional[str] = None, index: Optional[int] = None):
    """定位片段 → (track_name, track, index, segment). 找不到抛 ValueError."""
    if segment_id:
        for tname, track in _all_tracks(script):
            for i, seg in enumerate(track.segments):
                if getattr(seg, "segment_id", None) == segment_id:
                    return tname, track, i, seg
        raise ValueError(f"找不到 segment_id={segment_id} 的片段")
    if track_name is None or index is None:
        raise ValueError("需要提供 segment_id, 或同时提供 track_name 与 index")
    for tname, track in _all_tracks(script):
        if tname != track_name:
            continue
        if not 0 <= index < len(track.segments):
            raise ValueError(f"轨道 '{track_name}' 没有 index={index} 的片段 (共 {len(track.segments)} 段)")
        return tname, track, index, track.segments[index]
    raise ValueError(f"找不到轨道 '{track_name}'")


def _locate(args) -> Tuple[str, Any, int, Any]:
    """从 HTTP 参数定位片段."""
    seg = locate_segment(args.get('_script'),
                         segment_id=args.get('segment_id') or None,
                         track_name=args.get('track_name') or None,
                         index=args.get('index'))
    return seg


def _recompute_duration(script) -> None:
    max_duration = 0
    for _tname, track in _all_tracks(script):
        for seg in track.segments:
            try:
                max_duration = max(max_duration, seg.end)
            except Exception:
                continue
    script.duration = max_duration


def _seg_summary(tname, i, seg) -> Dict[str, Any]:
    return {
        'track': tname, 'index': i, 'segment_id': getattr(seg, 'segment_id', None),
        'start_s': round(getattr(seg, 'start', 0) / SEC, 3),
        'end_s': round(getattr(seg, 'end', 0) / SEC, 3),
        'duration_s': round(getattr(seg, 'duration', 0) / SEC, 3),
    }


# ===========================================================================
# Phase 3: 库层现成能力的包装
# ===========================================================================

def update_segment_impl(draft_id: str, segment_id: str = None, track_name: str = None,
                        index: int = None, start: float = None, duration: float = None,
                        end: float = None, speed: float = None, volume: float = None,
                        alpha: float = None, rotation: float = None,
                        scale_x: float = None, scale_y: float = None,
                        transform_x: float = None, transform_y: float = None,
                        flip_horizontal: bool = None, flip_vertical: bool = None) -> Dict[str, Any]:
    """修改已存在片段的时间/变换/速度/音量.
    start/end/duration 是成片时间轴秒; start+duration 同时给则以 duration 为准,
    end 与 duration 同时给则 duration 优先. 变换参数即 Clip_settings 8 字段."""
    script = _get_script(draft_id)
    tname, track, i, seg = locate_segment(script, segment_id=segment_id or None,
                                          track_name=track_name, index=index)
    changed = []
    if start is not None:
        seg.start = int(start * SEC)
        changed.append('start')
    if duration is not None:
        seg.duration = int(duration * SEC)
        changed.append('duration')
    elif end is not None and start is not None:
        seg.duration = max(0, int((end - start) * SEC))
        changed.append('duration')
    elif end is not None:
        seg.duration = max(0, int(end * SEC) - seg.start)
        changed.append('duration')
    if speed is not None and hasattr(seg, 'speed') and seg.speed is not None:
        seg.speed.speed = speed
        changed.append('speed')
    if volume is not None and hasattr(seg, 'volume'):
        seg.volume = volume
        changed.append('volume')
    clip = getattr(seg, 'clip_settings', None) or getattr(seg, 'clip', None)
    if clip is not None:
        for key, val in (('alpha', alpha), ('rotation', rotation),
                         ('scale_x', scale_x), ('scale_y', scale_y),
                         ('transform_x', transform_x), ('transform_y', transform_y),
                         ('flip_horizontal', flip_horizontal), ('flip_vertical', flip_vertical)):
            if val is not None and hasattr(clip, key):
                setattr(clip, key, val)
                changed.append(key)
    _recompute_duration(script)
    out = _seg_summary(tname, i, seg)
    out.update({'ok': True, 'changed': changed, 'draft_id': draft_id})
    if not changed:
        out['note'] = '没有字段被修改 (检查参数名/片段类型是否支持该属性)'
    return out


def replace_material_impl(draft_id: str, old_material_name: str, new_url: str,
                          replace_crop: bool = False) -> Dict[str, Any]:
    """按素材名替换素材文件 (所有引用它的片段同步换), 时间线不变.
    新素材类型按旧素材类型决定 (视频↔视频, 音频↔音频)."""
    import pyJianYingDraft as draft
    script = _get_script(draft_id)
    new_url = (new_url or '').strip()
    if not new_url:
        return {'ok': False, 'error': 'new_url 不能为空'}
    # 判断旧素材类型, 构造对应新素材对象
    mats = script.materials
    video_hit = any((getattr(m, 'material_name', None) or (m.get('material_name') if isinstance(m, dict) else None)) == old_material_name
                    for m in (getattr(mats, 'videos', None) or []))
    audio_hit = any((getattr(m, 'material_name', None) or (m.get('material_name') if isinstance(m, dict) else None)) == old_material_name
                    for m in (getattr(mats, 'audios', None) or []))
    # 导入素材列表也查 (load_template 载入的草稿素材在 imported_materials)
    imported = getattr(script, 'imported_materials', {}) or {}
    for m in (imported.get('videos') or []):
        if isinstance(m, dict) and m.get('material_name') == old_material_name:
            video_hit = True
    for m in (imported.get('audios') or []):
        if isinstance(m, dict) and m.get('material_name') == old_material_name:
            audio_hit = True
    try:
        if video_hit:
            # 第一参数是 material_type('video'/'photo'), 图片素材必须传 'photo'
            ext = os.path.splitext(new_url)[1].lower()
            mtype = 'photo' if ext in ('.jpg', '.jpeg', '.png', '.webp', '.bmp') else 'video'
            material = draft.Video_material(mtype, path=new_url)
        elif audio_hit:
            material = draft.Audio_material(new_url)
        else:
            return {'ok': False, 'error': f"素材 '{old_material_name}' 未找到 (既不是视频也不是音频素材)"}
        # 手动替换全部匹配条目 (库层 replace_material_by_name 遇重名素材抛
        # AmbiguousMaterial; 同内容素材重复出现是常态, 全部换新才对)
        replaced = 0
        name_key = 'material_name' if video_hit else 'name'
        target_lists = []
        if video_hit:
            target_lists.append(getattr(script.materials, 'videos', None) or [])
            target_lists.append(imported.get('videos') or [])
        else:
            target_lists.append(getattr(script.materials, 'audios', None) or [])
            target_lists.append(imported.get('audios') or [])

        # 新素材必须归位到草稿 assets/ 内: 直接写草稿外绝对路径, zip 打包时不含该文件,
        # 渲染节点注入后素材缺失 → 剪映弹"草稿丢失"横幅吞掉卡片点击 (8 连败实锤).
        import shutil as _shutil
        here = os.path.dirname(os.path.abspath(__file__))
        draft_dir = os.path.join(here, draft_id)
        type_dir = 'audio' if audio_hit else ('image' if (video_hit and mtype == 'photo') else 'video')
        in_draft = os.path.join(draft_dir, 'assets', type_dir, material.material_name)
        os.makedirs(os.path.dirname(in_draft), exist_ok=True)
        if os.path.abspath(new_url) != os.path.abspath(in_draft):
            _shutil.copyfile(new_url, in_draft)
        in_draft_fwd = in_draft.replace('\\', '/')

        for lst in target_lists:
            for mat in lst:
                if not isinstance(mat, dict) or mat.get(name_key) != old_material_name:
                    continue
                mat[name_key] = material.material_name
                mat['path'] = in_draft_fwd
                mat['duration'] = material.duration
                if video_hit:
                    mat['width'] = material.width
                    mat['height'] = material.height
                    mat['material_type'] = material.material_type
                replaced += 1
        if not replaced:
            return {'ok': False, 'error': f"素材 '{old_material_name}' 在素材表中未找到条目"}
        return {'ok': True, 'draft_id': draft_id, 'replaced': old_material_name,
                'new_url': new_url, 'replaced_count': replaced}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def update_text_impl(draft_id: str, text: str, segment_id: str = None,
                     track_name: str = None, index: int = None) -> Dict[str, Any]:
    """修改已存在文本片段的文字内容.
    首选库层 replace_text (imported text 轨); 普通 add_text/add_subtitle 建的 text 轨
    库层不支持 → 回退直接改文字素材 content JSON 里的 text 字段."""
    import json as _json
    script = _get_script(draft_id)
    tname, track, i, seg = locate_segment(script, segment_id=segment_id or None,
                                          track_name=track_name, index=index)
    try:
        script.replace_text(track, i, text)
        return {'ok': True, 'draft_id': draft_id, 'track': tname, 'index': i,
                'text': text[:100], 'via': 'replace_text'}
    except TypeError:
        pass  # 普通 text 轨 → 走素材直改
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    # 直改素材 content (materials.texts 或 imported_materials['texts'] 里找 material_id)
    mid = getattr(seg, 'material_id', None)
    candidates = list(getattr(script.materials, 'texts', None) or [])
    imported = getattr(script, 'imported_materials', {}) or {}
    candidates.extend(imported.get('texts') or [])
    for m in candidates:
        m_id = m.get('id') if isinstance(m, dict) else getattr(m, 'id', None)
        if m_id != mid:
            continue
        content = m.get('content') if isinstance(m, dict) else getattr(m, 'content', None)
        try:
            obj = _json.loads(content) if isinstance(content, str) else dict(content or {})
        except Exception:
            return {'ok': False, 'error': '文字素材 content 解析失败'}
        obj['text'] = text
        new_content = _json.dumps(obj, ensure_ascii=False)
        if isinstance(m, dict):
            m['content'] = new_content
        else:
            m.content = new_content
        return {'ok': True, 'draft_id': draft_id, 'track': tname, 'index': i,
                'text': text[:100], 'via': 'material_content'}
    return {'ok': False, 'error': f'找不到片段的文字素材 (material_id={mid})'}


def add_fade_impl(draft_id: str, in_duration: float, out_duration: float,
                  segment_id: str = None, track_name: str = None, index: int = None) -> Dict[str, Any]:
    """给音频片段加淡入淡出 (秒)."""
    script = _get_script(draft_id)
    tname, track, i, seg = locate_segment(script, segment_id=segment_id or None,
                                          track_name=track_name, index=index)
    if not hasattr(seg, 'add_fade'):
        return {'ok': False, 'error': '目标片段不是音频段 (只有音频段支持淡入淡出)'}
    try:
        seg.add_fade(int(in_duration * SEC), int(out_duration * SEC))
        return {'ok': True, 'draft_id': draft_id, 'track': tname, 'index': i,
                'fade_in_s': in_duration, 'fade_out_s': out_duration}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _resolve_enum(enum_cls, name: str):
    """按名字找枚举成员 (大小写/空格下划线宽松匹配)."""
    if not name:
        return None
    key = name.strip().lower().replace(' ', '_')
    for m in enum_cls:
        if m.name.lower() == key or (m.value and str(m.value).lower() == name.lower()):
            return m
    return None


def add_filter_impl(draft_id: str, filter_name: str, intensity: float = 100.0,
                    segment_id: str = None, track_name: str = None, index: int = None) -> Dict[str, Any]:
    """给视频/图片段加滤镜 (名称用 list_edit_enums 查, 如 '胶片''清新')."""
    import pyJianYingDraft as draft
    script = _get_script(draft_id)
    tname, track, i, seg = locate_segment(script, segment_id=segment_id or None,
                                          track_name=track_name, index=index)
    if not hasattr(seg, 'add_filter'):
        return {'ok': False, 'error': '目标片段不支持滤镜 (仅视频/图片段)'}
    from pyJianYingDraft.metadata import Filter_type
    f = _resolve_enum(Filter_type, filter_name)
    if f is None:
        names = sorted(m.name for m in Filter_type)[:30]
        return {'ok': False, 'error': f"滤镜 '{filter_name}' 不存在, 部分可选: {names}"}
    try:
        seg.add_filter(f, intensity=intensity)
        return {'ok': True, 'draft_id': draft_id, 'track': tname, 'index': i, 'filter': f.name}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def add_transition_impl(draft_id: str, transition_name: str, duration: float = None,
                        segment_id: str = None, track_name: str = None, index: int = None) -> Dict[str, Any]:
    """给视频/图片段加转场 (作用于该段与下一段之间). 名称用 list_edit_enums(kind='transition') 查."""
    script = _get_script(draft_id)
    tname, track, i, seg = locate_segment(script, segment_id=segment_id or None,
                                          track_name=track_name, index=index)
    if not hasattr(seg, 'add_transition'):
        return {'ok': False, 'error': '目标片段不支持转场 (仅视频/图片段)'}
    from pyJianYingDraft.metadata import Transition_type
    t = _resolve_enum(Transition_type, transition_name)
    if t is None:
        names = sorted(m.name for m in Transition_type)[:30]
        return {'ok': False, 'error': f"转场 '{transition_name}' 不存在, 部分可选: {names}"}
    try:
        seg.add_transition(t, duration=int(duration * SEC) if duration else None)
        return {'ok': True, 'draft_id': draft_id, 'track': tname, 'index': i, 'transition': t.name}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def add_animation_impl(draft_id: str, animation_name: str, kind: str = 'intro',
                       duration: float = None,
                       segment_id: str = None, track_name: str = None, index: int = None) -> Dict[str, Any]:
    """给视频/图片段加入场(intro)/出场(outro)/组合(combo)动画."""
    script = _get_script(draft_id)
    tname, track, i, seg = locate_segment(script, segment_id=segment_id or None,
                                          track_name=track_name, index=index)
    if not hasattr(seg, 'add_animation'):
        return {'ok': False, 'error': '目标片段不支持动画 (仅视频/图片段)'}
    from pyJianYingDraft.metadata import Intro_type, Outro_type, Group_animation_type
    cls_map = {'intro': Intro_type, 'outro': Outro_type, 'combo': Group_animation_type}
    enum_cls = cls_map.get(kind)
    if enum_cls is None:
        return {'ok': False, 'error': "kind 必须是 intro/outro/combo"}
    a = _resolve_enum(enum_cls, animation_name)
    if a is None:
        names = sorted(m.name for m in enum_cls)[:30]
        return {'ok': False, 'error': f"动画 '{animation_name}' 不存在, 部分可选: {names}"}
    try:
        kwargs = {}
        if duration is not None:
            kwargs['duration'] = int(duration * SEC)
        seg.add_animation(a, **kwargs)
        return {'ok': True, 'draft_id': draft_id, 'track': tname, 'index': i,
                'kind': kind, 'animation': a.name}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


# ===========================================================================
# Phase 4: 自研编辑 (底层完全没有)
# ===========================================================================

def split_segment_impl(draft_id: str, at: float, segment_id: str = None,
                       track_name: str = None, index: int = None) -> Dict[str, Any]:
    """在成片时间点 at(秒) 把一段拆成两段 (深拷贝, 前段截断, 后段起点=at).
    素材源区间 (source_timerange) 按比例同步切分 — 视频/音频段的截取保持连续."""
    script = _get_script(draft_id)
    tname, track, i, seg = locate_segment(script, segment_id=segment_id or None,
                                          track_name=track_name, index=index)
    at_us = int(at * SEC)
    if not (seg.start < at_us < seg.end):
        return {'ok': False, 'error': f'切点 {at}s 不在片段区间 [{seg.start / SEC:.3f}, {seg.end / SEC:.3f}] 内'}
    tail = copy.deepcopy(seg)
    import uuid as _uuid
    tail.segment_id = _uuid.uuid4().hex
    # 源区间同步切 (视频/音频有 source_timerange; 图片/文本没有则不用管)
    src = getattr(seg, 'source_timerange', None)
    if src is not None and getattr(seg, 'duration', 0):
        ratio = (at_us - seg.start) / seg.duration
        cut_src = src.start + int(src.duration * ratio)
        tail.source_timerange = type(src)(cut_src, src.end - cut_src)
        seg.source_timerange = type(src)(src.start, cut_src - src.start)
    tail.start = at_us
    tail.duration = seg.end - at_us
    seg.duration = at_us - seg.start
    track.segments.insert(i + 1, tail)
    _recompute_duration(script)
    return {'ok': True, 'draft_id': draft_id, 'track': tname,
            'head': _seg_summary(tname, i, seg), 'tail': _seg_summary(tname, i + 1, tail)}


def duplicate_segment_impl(draft_id: str, segment_id: str = None, track_name: str = None,
                           index: int = None, offset: float = None,
                           to_track: str = None, at: float = None) -> Dict[str, Any]:
    """复制片段 (新 segment_id). offset=相对原位置偏移秒; at=指定落到成片时间轴的秒;
    to_track=放到别的轨道. 默认落在原片段正后方."""
    script = _get_script(draft_id)
    tname, track, i, seg = locate_segment(script, segment_id=segment_id or None,
                                          track_name=track_name, index=index)
    clone = copy.deepcopy(seg)
    import uuid as _uuid
    clone.segment_id = _uuid.uuid4().hex
    if at is not None:
        clone.start = int(at * SEC)
    elif offset is not None:
        clone.start = seg.start + int(offset * SEC)
    else:
        clone.start = seg.end
    if to_track:
        for tname2, track2 in _all_tracks(script):
            if tname2 == to_track:
                track2.segments.append(clone)
                track2.segments.sort(key=lambda s: s.start)
                _recompute_duration(script)
                return {'ok': True, 'draft_id': draft_id, 'track': to_track,
                        'cloned': _seg_summary(to_track, track2.segments.index(clone), clone)}
        return {'ok': False, 'error': f"目标轨道 '{to_track}' 不存在"}
    track.segments.insert(i + 1, clone)
    _recompute_duration(script)
    return {'ok': True, 'draft_id': draft_id, 'track': tname,
            'cloned': _seg_summary(tname, i + 1, clone)}


def move_segment_impl(draft_id: str, to: float, segment_id: str = None,
                      track_name: str = None, index: int = None,
                      to_track: str = None) -> Dict[str, Any]:
    """搬移片段: to=新的成片起始秒. to_track 可同时换轨道 (重叠校验: 目标区间与其他段冲突则拒绝)."""
    script = _get_script(draft_id)
    tname, track, i, seg = locate_segment(script, segment_id=segment_id or None,
                                          track_name=track_name, index=index)
    to_us = int(to * SEC)
    new_start, new_end = to_us, to_us + seg.duration
    # 重叠校验 (排除自身)
    target_track = track
    if to_track:
        for tname2, track2 in _all_tracks(script):
            if tname2 == to_track:
                target_track = track2
                break
        else:
            return {'ok': False, 'error': f"目标轨道 '{to_track}' 不存在"}
    for j, other in enumerate(target_track.segments):
        if target_track is track and j == i:
            continue
        if other.start < new_end and new_start < other.end:
            return {'ok': False,
                    'error': f'目标区间 [{new_start / SEC:.3f}, {new_end / SEC:.3f}] 与该轨道另一段 '
                             f'[{other.start / SEC:.3f}, {other.end / SEC:.3f}] 重叠'}
    if target_track is not track:
        track.segments.pop(i)
        target_track.segments.append(seg)
    seg.start = new_start
    target_track.segments.sort(key=lambda s: s.start)
    _recompute_duration(script)
    tname_out = to_track or tname
    return {'ok': True, 'draft_id': draft_id, 'track': tname_out,
            'moved': _seg_summary(tname_out, target_track.segments.index(seg), seg)}


def reorder_track_impl(draft_id: str, track_name: str, relative_index: int) -> Dict[str, Any]:
    """调整轨道层级 (render_index): 数值越大越靠上层显示."""
    script = _get_script(draft_id)
    for tname, track in _all_tracks(script):
        if tname == track_name:
            track.render_index = relative_index
            return {'ok': True, 'draft_id': draft_id, 'track': track_name,
                    'relative_index': relative_index}
    return {'ok': False, 'error': f"轨道 '{track_name}' 不存在"}
