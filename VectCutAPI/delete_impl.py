"""草稿编辑能力实现: 查询 / 删除片段 / 删除轨道

给 agent 提供编辑能力 (之前只有 create/add)。本模块直接操作 DRAFT_CACHE 中存活
的 Script_file 对象, 复用 save_draft_impl.update_media_metadata 里已有的"删除片段
+ 重算时长"模式 (见 save_draft_impl.py:487-520)。

删除后必须做两件清理:
  1. 清理孤儿素材: 扫描所有剩余片段引用到的 material_id + extra_material_refs,
     把 Script_material 各列表里不再被任何片段引用的素材剔除, 避免剪映加载到悬挂引用。
  2. 重算 script.duration = max(所有剩余片段的 end)。

时间单位: 库内部用微秒, HTTP/MCP API 用秒。本模块函数统一接收秒, 内部转微秒。
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from draft_cache import DRAFT_CACHE

logger = logging.getLogger(__name__)

SEC = 1_000_000  # 一秒 = 1e6 微秒


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _get_script(draft_id: str):
    """从缓存取出存活的 Script_file, 不存在则抛 KeyError。"""
    if draft_id not in DRAFT_CACHE:
        raise KeyError(f"草稿 '{draft_id}' 不存在于缓存中 (可能未创建或已过期)")
    return DRAFT_CACHE[draft_id]


def _all_segments(script) -> List[Tuple[Any, Any]]:
    """返回 [(track_name, segment), ...], 遍历普通轨道 + 导入轨道。"""
    pairs: List[Tuple[Any, Any]] = []
    for track_name, track in script.tracks.items():
        for seg in track.segments:
            pairs.append((track_name, seg))
    for track in getattr(script, "imported_tracks", []) or []:
        for seg in track.segments:
            pairs.append((track.name, seg))
    return pairs


def _segment_referenced_ids(seg) -> List[str]:
    """收集一个片段引用到的所有素材 id: material_id + extra_material_refs。"""
    ids: List[str] = []
    mid = getattr(seg, "material_id", None)
    if mid:
        ids.append(mid)
    extra = getattr(seg, "extra_material_refs", None)
    if extra:
        ids.extend(extra)
    return ids


def _recompute_duration(script) -> None:
    """重算 script.duration = 所有剩余片段 end 的最大值。"""
    max_duration = 0
    for _name, seg in _all_segments(script):
        try:
            max_duration = max(max_duration, seg.end)
        except Exception:
            continue
    script.duration = max_duration
    logger.info(f"重算草稿总时长: {script.duration} 微秒 ({script.duration / SEC:.3f}s)")


def _cleanup_orphan_materials(script) -> Dict[str, int]:
    """删除 Script_material 各列表中不再被任何片段引用的素材。

    返回 {列表名: 删除数量} 的统计。

    各列表里素材的"id 字段"映射关系:
      videos       -> material_id   (Video_material)
      audios       -> material_id   (Audio_material)
      stickers     -> id            (dict)
      texts        -> id            (dict)
      animations   -> animation_id  (Segment_animations)
      video_effects-> global_id     (Video_effect)
      audio_effects-> effect_id     (Audio_effect)
      audio_fades  -> fade_id       (Audio_fade)
      speeds       -> global_id     (Speed)
      masks        -> id            (dict)
      transitions  -> global_id     (Transition)
      filters      -> global_id     (Filter / TextBubble / TextEffect)
      canvases     -> global_id     (BackgroundFilling)
    """
    materials = script.materials

    # 字段名映射: (列表属性名, 取 id 的字段名)
    lists_and_id_field: List[Tuple[str, str]] = [
        ("videos", "material_id"),
        ("audios", "material_id"),
        ("stickers", "id"),
        ("texts", "id"),
        ("animations", "animation_id"),
        ("video_effects", "global_id"),
        ("audio_effects", "effect_id"),
        ("audio_fades", "fade_id"),
        ("speeds", "global_id"),
        ("masks", "id"),
        ("transitions", "global_id"),
        ("filters", "global_id"),
        ("canvases", "global_id"),
    ]

    # 收集所有剩余片段引用到的 id 集合
    referenced: set = set()
    for _name, seg in _all_segments(script):
        referenced.update(_segment_referenced_ids(seg))

    stats: Dict[str, int] = {}
    for list_name, id_field in lists_and_id_field:
        lst = getattr(materials, list_name, None)
        if not lst:
            stats[list_name] = 0
            continue
        before = len(lst)
        kept = []
        for item in lst:
            # item 可能是对象 (有属性) 或 dict
            if isinstance(item, dict):
                item_id = item.get(id_field)
            else:
                item_id = getattr(item, id_field, None)
            if item_id is not None and item_id in referenced:
                kept.append(item)
            else:
                logger.debug(f"清理孤儿素材: list={list_name} id={item_id}")
        setattr(materials, list_name, kept)
        stats[list_name] = before - len(kept)

    return stats


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def query_draft_impl(draft_id: str) -> Dict[str, Any]:
    """返回对 agent 友好的草稿结构摘要 (轨道 / 片段 / 时长)。

    不返回完整 script JSON (那是 /query_script 的职责), 只给 agent 足够信息定位
    要编辑的片段: 每个轨道的类型、名称、片段数, 每个片段的 index/segment_id/
    时间范围/类型/素材摘要。
    """
    script = _get_script(draft_id)
    tracks_out: List[Dict[str, Any]] = []

    def _describe_track(track, name: str, is_imported: bool) -> Dict[str, Any]:
        segs: List[Dict[str, Any]] = []
        for idx, seg in enumerate(track.segments):
            seg_info: Dict[str, Any] = {
                "index": idx,
                "segment_id": getattr(seg, "segment_id", None),
                "track_name": name,
                "start": round(seg.start / SEC, 3),
                "end": round(seg.end / SEC, 3),
                "duration": round(seg.duration / SEC, 3),
                "type": type(seg).__name__,
            }
            # 文本片段带内容, 方便 agent 按内容定位
            text = getattr(seg, "text", None)
            if text is not None:
                seg_info["text"] = text
            # 媒体片段带素材 id
            mid = getattr(seg, "material_id", None)
            if mid:
                seg_info["material_id"] = mid
            segs.append(seg_info)
        return {
            "track_name": name,
            "track_type": track.track_type.name,
            "render_index": track.render_index,
            "segments": segs,
            "is_imported": is_imported,
        }

    for name, track in script.tracks.items():
        tracks_out.append(_describe_track(track, name, False))
    for track in getattr(script, "imported_tracks", []) or []:
        tracks_out.append(_describe_track(track, track.name, True))

    return {
        "draft_id": draft_id,
        "width": script.width,
        "height": script.height,
        "fps": script.fps,
        "duration_sec": round(script.duration / SEC, 3),
        "track_count": len(tracks_out),
        "tracks": tracks_out,
    }


# ---------------------------------------------------------------------------
# 删除片段
# ---------------------------------------------------------------------------

def delete_segment_impl(
    draft_id: str,
    track_name: Optional[str] = None,
    index: Optional[int] = None,
    segment_id: Optional[str] = None,
) -> Dict[str, Any]:
    """从草稿删除一个片段。

    定位方式 (按优先级):
      1. segment_id: 精确匹配 (track_name 可选, 用于消歧)
      2. track_name + index: 指定轨道第 index 个片段
      3. track_name + 时间: 见下方 (本函数不直接支持, 用 query_draft 先拿到 index)

    删除后: 清理孤儿素材 + 重算时长。
    """
    script = _get_script(draft_id)

    # 候选轨道: 普通轨道 + 导入轨道
    candidate_tracks = []  # [(track_obj, is_imported)]
    if track_name is not None:
        if track_name in script.tracks:
            candidate_tracks.append((script.tracks[track_name], False))
        for tr in getattr(script, "imported_tracks", []) or []:
            if tr.name == track_name:
                candidate_tracks.append((tr, True))
        if not candidate_tracks:
            raise KeyError(f"不存在名为 '{track_name}' 的轨道")
    else:
        for tr in script.tracks.values():
            candidate_tracks.append((tr, False))
        for tr in getattr(script, "imported_tracks", []) or []:
            candidate_tracks.append((tr, True))

    # 定位到具体 (track, index)
    target_track = None
    target_idx = None
    target_seg = None

    if segment_id is not None:
        for tr, _imp in candidate_tracks:
            for i, seg in enumerate(tr.segments):
                if getattr(seg, "segment_id", None) == segment_id:
                    target_track, target_idx, target_seg = tr, i, seg
                    break
            if target_seg is not None:
                break
        if target_seg is None:
            raise KeyError(f"未找到 segment_id='{segment_id}' 的片段"
                           + (f" (轨道 '{track_name}')" if track_name else ""))
    elif index is not None:
        if track_name is None:
            raise ValueError("按 index 删除时必须同时提供 track_name")
        # candidate_tracks 此时只有一条 (track_name 唯一)
        tr = candidate_tracks[0][0]
        if index < 0 or index >= len(tr.segments):
            raise IndexError(f"轨道 '{track_name}' 的 index {index} 越界 "
                             f"(共 {len(tr.segments)} 个片段)")
        target_track, target_idx, target_seg = tr, index, tr.segments[index]
    else:
        raise ValueError("必须提供 segment_id 或 (track_name + index) 之一来定位片段")

    removed_desc = _describe_segment(target_seg, target_track.name, target_idx)

    # 执行删除
    target_track.segments.pop(target_idx)
    logger.info(f"已删除片段: draft={draft_id} track='{target_track.name}' index={target_idx}")

    # 清理 + 重算
    orphan_stats = _cleanup_orphan_materials(script)
    _recompute_duration(script)

    return {
        "draft_id": draft_id,
        "deleted": removed_desc,
        "orphan_materials_removed": orphan_stats,
        "duration_sec": round(script.duration / SEC, 3),
    }


# ---------------------------------------------------------------------------
# 删除轨道
# ---------------------------------------------------------------------------

def delete_track_impl(
    draft_id: str,
    track_name: Optional[str] = None,
    track_id: Optional[str] = None,
    delete_all: bool = False,
) -> Dict[str, Any]:
    """删除一整条轨道 (含其上所有片段)。

    定位方式 (按优先级):
      1. track_id: 精确匹配 (同名轨道消歧, 从 get_draft_timeline 拿 track_id)
      2. delete_all=True + track_name: 删掉所有同名轨道 (批量)
      3. track_name (兼容旧用法): 删第一个匹配项; 同名 >1 时返回 ambiguous=True

    删除后清理孤儿素材 + 重算时长。
    """
    script = _get_script(draft_id)
    if not track_id and not track_name:
        raise ValueError("track_name 或 track_id 至少提供一个")

    # --- 1. 按 track_id 精确删除 (dict + list 都查) ---
    if track_id:
        removed = None
        is_imported = False
        # dict 轨道
        for nm, tr in list(script.tracks.items()):
            if getattr(tr, "track_id", None) == track_id:
                removed = script.tracks.pop(nm)
                is_imported = False
                break
        if removed is None:
            imported = getattr(script, "imported_tracks", []) or []
            idx = next((i for i, tr in enumerate(imported)
                        if getattr(tr, "track_id", None) == track_id), None)
            if idx is not None:
                removed = imported.pop(idx)
                is_imported = True
        if removed is None:
            raise KeyError(f"不存在 track_id='{track_id}' 的轨道")

        logger.info(f"已删除轨道(by id): draft={draft_id} track_id='{track_id}' "
                    f"name='{removed.name}' imported={is_imported}")
        orphan_stats = _cleanup_orphan_materials(script)
        _recompute_duration(script)
        return {
            "draft_id": draft_id,
            "deleted_track": {
                "track_name": removed.name,
                "track_type": removed.track_type.name,
                "track_id": getattr(removed, "track_id", None),
                "segment_count": len(removed.segments),
                "is_imported": is_imported,
            },
            "orphan_materials_removed": orphan_stats,
            "duration_sec": round(script.duration / SEC, 3),
        }

    # --- 2/3. 按 track_name ---
    # 先统计同名候选数 (dict + list 合计)
    dict_matches = [nm for nm, tr in script.tracks.items() if tr.name == track_name]
    list_matches = [tr for tr in (getattr(script, "imported_tracks", []) or []) if tr.name == track_name]
    total_matches = len(dict_matches) + len(list_matches)
    if total_matches == 0:
        raise KeyError(f"不存在名为 '{track_name}' 的轨道")

    if delete_all:
        # 批量删所有同名
        deleted: List[Dict[str, Any]] = []
        for nm in dict_matches:
            tr = script.tracks.pop(nm)
            deleted.append({
                "track_name": tr.name, "track_type": tr.track_type.name,
                "track_id": getattr(tr, "track_id", None),
                "segment_count": len(tr.segments), "is_imported": False,
            })
        imported = getattr(script, "imported_tracks", []) or []
        for tr in list_matches:
            idx = next((i for i, t in enumerate(imported) if t is tr), None)
            if idx is not None:
                imported.pop(idx)
                deleted.append({
                    "track_name": tr.name, "track_type": tr.track_type.name,
                    "track_id": getattr(tr, "track_id", None),
                    "segment_count": len(tr.segments), "is_imported": True,
                })
        logger.info(f"已删除同名轨道(all): draft={draft_id} name='{track_name}' "
                    f"count={len(deleted)}")
        orphan_stats = _cleanup_orphan_materials(script)
        _recompute_duration(script)
        return {
            "draft_id": draft_id,
            "deleted_tracks": deleted,
            "deleted_count": len(deleted),
            "ambiguous": total_matches > 1,
            "orphan_materials_removed": orphan_stats,
            "duration_sec": round(script.duration / SEC, 3),
        }

    # --- 3. 兼容旧用法: 删第一个匹配, 同名>1 标 ambiguous ---
    removed_track = None
    is_imported = False
    if dict_matches:
        removed_track = script.tracks.pop(dict_matches[0])
        is_imported = False
    else:
        imported = getattr(script, "imported_tracks", []) or []
        for i, tr in enumerate(imported):
            if tr.name == track_name:
                removed_track = imported.pop(i)
                is_imported = True
                break

    logger.info(f"已删除轨道: draft={draft_id} track='{track_name}' "
                f"segments={len(removed_track.segments)} imported={is_imported} "
                f"ambiguous={total_matches > 1}")

    orphan_stats = _cleanup_orphan_materials(script)
    _recompute_duration(script)
    return {
        "draft_id": draft_id,
        "deleted_track": {
            "track_name": track_name,
            "track_type": removed_track.track_type.name,
            "track_id": getattr(removed_track, "track_id", None),
            "segment_count": len(removed_track.segments),
            "is_imported": is_imported,
        },
        "ambiguous": total_matches > 1,
        "orphan_materials_removed": orphan_stats,
        "duration_sec": round(script.duration / SEC, 3),
    }


# ---------------------------------------------------------------------------
# 删除所有空轨道
# ---------------------------------------------------------------------------

def delete_empty_tracks_impl(
    draft_id: str,
    track_type: Optional[str] = None,
    track_name: Optional[str] = None,
) -> Dict[str, Any]:
    """删除所有零片段的空轨道。

    可选过滤 track_type (如 'video') / track_name 进一步缩小范围。
    删除按对象身份操作 (不按名字), 因此同名空轨也能逐条精确删除, 不会误删另一条
    或因 pop 下标错位。删除后清理孤儿素材 + 重算时长 (复用现有 helper)。

    返回 deleted_tracks 列表, 每条带 track_id (与 get_draft_timeline 暴露的 id 对应)。
    """
    script = _get_script(draft_id)

    # 候选: 普通轨道(dict) + 导入轨道(list), 标注来源
    candidates: List[Tuple[Any, bool]] = []  # [(track_obj, is_imported)]
    for tr in script.tracks.values():
        candidates.append((tr, False))
    for tr in getattr(script, "imported_tracks", []) or []:
        candidates.append((tr, True))

    def _matches(track) -> bool:
        if len(track.segments) != 0:
            return False
        if track_type is not None and track.track_type.name != track_type:
            return False
        if track_name is not None and track.name != track_name:
            return False
        return True

    to_delete = [(tr, imp) for tr, imp in candidates if _matches(tr)]

    deleted: List[Dict[str, Any]] = []
    # 按身份删除, 避免 pop 时下标错位 / 同名误删
    for track, is_imported in to_delete:
        if is_imported:
            imported = getattr(script, "imported_tracks", []) or []
            idx = next((i for i, tr in enumerate(imported) if tr is track), None)
            if idx is None:
                continue  # 理论不会发生
            imported.pop(idx)
        else:
            # dict 轨道按名字 pop (dict 轨道名字唯一)
            script.tracks.pop(track.name, None)
        deleted.append({
            "track_name": track.name,
            "track_type": track.track_type.name,
            "track_id": getattr(track, "track_id", None),
            "is_imported": is_imported,
        })

    logger.info(f"已删除空轨道: draft={draft_id} count={len(deleted)} "
                f"names={[d['track_name'] for d in deleted]}")

    orphan_stats = _cleanup_orphan_materials(script)
    _recompute_duration(script)

    return {
        "draft_id": draft_id,
        "deleted_tracks": deleted,
        "deleted_count": len(deleted),
        "orphan_materials_removed": orphan_stats,
        "duration_sec": round(script.duration / SEC, 3),
    }


# ---------------------------------------------------------------------------
# 辅助描述
# ---------------------------------------------------------------------------

def _describe_segment(seg, track_name: str, index: int) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "segment_id": getattr(seg, "segment_id", None),
        "track_name": track_name,
        "index": index,
        "start": round(seg.start / SEC, 3),
        "end": round(seg.end / SEC, 3),
        "duration": round(seg.duration / SEC, 3),
        "type": type(seg).__name__,
    }
    text = getattr(seg, "text", None)
    if text is not None:
        info["text"] = text
    mid = getattr(seg, "material_id", None)
    if mid:
        info["material_id"] = mid
    return info
