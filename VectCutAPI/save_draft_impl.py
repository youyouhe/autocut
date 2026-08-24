import os
import pyJianYingDraft as draft
import shutil
from util import zip_draft, build_draft_asset_path
from oss import upload_to_oss
from typing import Dict, Literal
from draft_cache import DRAFT_CACHE
from save_task_cache import DRAFT_TASKS, get_task_status, update_tasks_cache, update_task_field, increment_task_field, update_task_fields, create_task
from downloader import download_audio, download_file, download_image, download_video
from concurrent.futures import ThreadPoolExecutor, as_completed
import imageio.v2 as imageio
import subprocess
import json
from get_duration_impl import get_video_duration
import uuid
import threading
from collections import OrderedDict
import time
import requests # Import requests for making HTTP calls
import logging
# Import configuration
from settings import IS_UPLOAD_DRAFT
from draft_profiles import get_draft_profile, write_profile_content

# --- Get your Logger instance ---
# The name here must match the logger name you configured in app.py
logger = logging.getLogger('flask_video_generator') 

# Define task status enumeration type
TaskStatus = Literal["initialized", "processing", "completed", "failed", "not_found"]

def build_asset_path(draft_folder: str, draft_id: str, asset_type: str, material_name: str) -> str:
    """
    Build asset file path
    :param draft_folder: Draft folder path
    :param draft_id: Draft ID
    :param asset_type: Asset type (audio, image, video)
    :param material_name: Material name
    :return: Built path
    """
    return build_draft_asset_path(draft_folder, draft_id, asset_type, material_name)

def _sanitize_script_for_export(script):
    """导出前清洗: 悬挂引用 + 素材表重复 id.
    load_template → save 的往返中, imported_materials 的动画/滤镜素材不会合并进
    script.materials, 但 import_track 会给 segments 重建 extra_material_refs ——
    不清掉就导出"引用存在但素材缺失"的毒草稿, 剪映首页直接不显示该卡片 (无报错).
    素材表重复 id 同理 (多次 save/merge 循环会指数复制素材条目)."""
    import json as _json
    mats = script.materials
    # 1. 全量 id 集合 (script.materials + imported_materials)
    valid_ids = set()
    def _collect(obj):
        if isinstance(obj, dict):
            for f in ('id', 'material_id', 'global_id', 'animation_id', 'effect_id', 'fade_id', 'resource_id'):
                if obj.get(f):
                    valid_ids.add(obj[f])
        else:
            for f in ('id', 'material_id', 'global_id', 'animation_id', 'effect_id', 'fade_id', 'resource_id'):
                v = getattr(obj, f, None)
                if v:
                    valid_ids.add(v)
    for list_name in ('videos', 'audios', 'stickers', 'texts', 'animations', 'video_effects',
                      'audio_effects', 'audio_fades', 'speeds', 'masks', 'transitions',
                      'filters', 'canvases'):
        for item in (getattr(mats, list_name, None) or []):
            _collect(item)
    imported = getattr(script, 'imported_materials', {}) or {}
    for mlist in imported.values():
        for item in (mlist or []):
            _collect(item)

    # 2. 清 segments 的悬挂 extra_material_refs (普通轨 + 导入轨)
    scrubbed = 0
    all_tracks = list(script.tracks.values()) + list(getattr(script, 'imported_tracks', []) or [])
    for track in all_tracks:
        for seg in (getattr(track, 'segments', None) or []):
            refs = getattr(seg, 'extra_material_refs', None)
            if refs:
                keep = [r for r in refs if r in valid_ids]
                if len(keep) != len(refs):
                    scrubbed += len(refs) - len(keep)
                    seg.extra_material_refs = keep

    # 3. 素材表去重 (按 id 保留第一个; script.materials 与 imported_materials 各自内部)
    deduped = 0
    def _dedupe_list(lst):
        nonlocal deduped
        seen, out = set(), []
        for item in lst:
            if isinstance(item, dict):
                mid = item.get('id') or item.get('material_id') or item.get('global_id')
            else:
                mid = (getattr(item, 'id', None) or getattr(item, 'material_id', None)
                       or getattr(item, 'global_id', None))
            if mid is not None:
                if mid in seen:
                    deduped += 1
                    continue
                seen.add(mid)
            out.append(item)
        return out
    for list_name in ('videos', 'audios', 'stickers', 'texts', 'animations', 'video_effects',
                      'audio_effects', 'audio_fades', 'speeds', 'masks', 'transitions',
                      'filters', 'canvases'):
        lst = getattr(mats, list_name, None)
        if lst:
            setattr(mats, list_name, _dedupe_list(lst))
    for k, mlist in imported.items():
        if isinstance(mlist, list):
            imported[k] = _dedupe_list(mlist)

    if scrubbed or deduped:
        logger.info(f'导出前清洗: 悬挂引用 -{scrubbed}, 重复素材 -{deduped}')
    return scrubbed, deduped


def save_draft_background(draft_id, draft_folder, task_id):
    """Background save draft to OSS"""
    try:
        # Get draft information from global cache
        # 与编辑操作共用草稿锁: save 重建目录期间禁止并发编辑 (edit_impl.draft_lock)
        try:
            from edit_impl import draft_lock as _draft_lock
            _dl = _draft_lock(draft_id)
            _dl.acquire()
        except Exception:
            _dl = None
        if draft_id not in DRAFT_CACHE:
            task_status = {
                "status": "failed",
                "message": f"Draft {draft_id} does not exist in cache",
                "progress": 0,
                "completed_files": 0,
                "total_files": 0,
                "draft_url": ""
            }
            update_tasks_cache(task_id, task_status)  # Use new cache management function
            logger.error(f"Draft {draft_id} does not exist in cache, task {task_id} failed.")
            if _dl is not None:
                try: _dl.release()
                except Exception: pass
            return
            
        script = DRAFT_CACHE[draft_id]
        logger.info(f"Successfully retrieved draft {draft_id} from cache.")
        
        # Update task status to processing
        task_status = {
            "status": "processing",
            "message": "Preparing draft files",
            "progress": 0,
            "completed_files": 0,
            "total_files": 0,
            "draft_url": ""
        }
        update_tasks_cache(task_id, task_status)  # Use new cache management function
        logger.info(f"Task {task_id} status updated to 'processing': Preparing draft files.")
        
        logger.info(f"Starting to save draft: {draft_id}")
        # Save draft
        current_dir = os.path.dirname(os.path.abspath(__file__))
        output_base_dir = draft_folder or current_dir
        draft_dir = os.path.join(output_base_dir, draft_id)

        # 替换旧草稿目录: 先原子 rename 挪走再尽力删除 —— 直接 rmtree 在并发写入时
        # (另一线程正在往里拷素材/写 json) 会 OSError [Errno 39] Directory not empty,
        # 且可能留下半删除状态. rename 是原子的, 旧目录改名后异步删除, 失败也无害.
        if os.path.exists(draft_dir):
            import time as _time
            old_dir = draft_dir + '.old_%d' % int(_time.time() * 1000)
            try:
                os.rename(draft_dir, old_dir)
                logger.warning(f"Moved existing draft folder aside: {old_dir}")
                import threading as _threading
                _threading.Thread(
                    target=lambda: shutil.rmtree(old_dir, ignore_errors=True),
                    daemon=True).start()
            except OSError:
                # rename 失败 (跨设备等极端情况) 退回 rmtree 尽力删
                shutil.rmtree(draft_dir, ignore_errors=True)

        # Choose different template directory based on configuration
        draft_profile = get_draft_profile()
        template_dir = draft_profile.template_dir
        template_source_dir = os.path.join(current_dir, template_dir)
        if not os.path.exists(template_source_dir):
            raise FileNotFoundError(f"Template draft {template_dir} does not exist")
        shutil.copytree(template_source_dir, draft_dir)
        
        # Update task status
        update_task_field(task_id, "message", "Updating media file metadata")
        update_task_field(task_id, "progress", 5)
        logger.info(f"Task {task_id} progress 5%: Updating media file metadata.")
        
        update_media_metadata(script, task_id)
        
        download_tasks = []
        failed_downloads = []  # 必须在 if download_tasks 外初始化: 本地素材(无下载任务)时
                               # 变量缺失会在下方 if failed_downloads 处 NameError, save 静默崩溃
                               # (2026-08-24 实锤: save 返回 success 但 0 字节落盘)
        
        audios = script.materials.audios
        if audios:
            for audio in audios:
                remote_url = audio.remote_url
                material_name = audio.material_name
                # Use helper function to build path
                if draft_folder:
                    audio.replace_path = build_asset_path(draft_folder, draft_id, "audio", material_name)
                if not remote_url:
                    local_p = getattr(audio, 'path', '') or ''
                    dst = os.path.join(output_base_dir, f"{draft_id}/assets/audio/{material_name}")
                    if local_p and os.path.isfile(local_p.replace('\\', '/')):
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copyfile(local_p.replace('\\', '/'), dst)
                        logger.info(f"Audio file {material_name} copied from local path.")
                    else:
                        failed_downloads.append({
                            'type': 'audio', 'material_name': material_name,
                            'remote_url': '', 'error': f'无 remote_url 且本地文件不存在: {local_p!r}'})
                    continue
                
                # Add audio download task
                download_tasks.append({
                    'type': 'audio',
                    'func': download_file,
                    'args': (remote_url, os.path.join(output_base_dir, f"{draft_id}/assets/audio/{material_name}")),
                    'material': audio
                })
        
        # Collect video and image download tasks
        videos = script.materials.videos
        if videos:
            for video in videos:
                remote_url = video.remote_url
                material_name = video.material_name
                
                if video.material_type == 'photo':
                    # Use helper function to build path
                    if draft_folder:
                        video.replace_path = build_asset_path(draft_folder, draft_id, "image", material_name)
                    if not remote_url:
                        # 本地素材: 从 .path 就地拷贝进 assets (历史行为是跳过,
                        # 导致重建的草稿目录无 assets, 渲染节点注入后素材丢失被拒导).
                        local_p = getattr(video, 'path', '') or ''
                        dst = os.path.join(output_base_dir, f"{draft_id}/assets/image/{material_name}")
                        if local_p and os.path.isfile(local_p.replace('\\', '/')):
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            shutil.copyfile(local_p.replace('\\', '/'), dst)
                            logger.info(f"Image file {material_name} copied from local path.")
                        else:
                            failed_downloads.append({
                                'type': 'image', 'material_name': material_name,
                                'remote_url': '', 'error': f'无 remote_url 且本地文件不存在: {local_p!r}'})
                        continue
                    
                    # Add image download task
                    download_tasks.append({
                        'type': 'image',
                        'func': download_file,
                        'args': (remote_url, os.path.join(output_base_dir, f"{draft_id}/assets/image/{material_name}")),
                        'material': video
                    })
                
                elif video.material_type == 'video':
                    # Use helper function to build path
                    if draft_folder:
                        video.replace_path = build_asset_path(draft_folder, draft_id, "video", material_name)
                    if not remote_url:
                        # 本地素材: 从 .path 就地拷贝进 assets (历史行为是跳过,
                        # 导致重建的草稿目录无 assets, 渲染节点注入后素材丢失被拒导).
                        local_p = getattr(video, 'path', '') or ''
                        dst = os.path.join(output_base_dir, f"{draft_id}/assets/video/{material_name}")
                        if local_p and os.path.isfile(local_p.replace('\\', '/')):
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            shutil.copyfile(local_p.replace('\\', '/'), dst)
                            logger.info(f"Video file {material_name} copied from local path.")
                        else:
                            failed_downloads.append({
                                'type': 'video', 'material_name': material_name,
                                'remote_url': '', 'error': f'无 remote_url 且本地文件不存在: {local_p!r}'})
                        continue
                    
                    # Add video download task
                    download_tasks.append({
                        'type': 'video',
                        'func': download_file,
                        'args': (remote_url, os.path.join(output_base_dir, f"{draft_id}/assets/video/{material_name}")),
                        'material': video
                    })

        # imported_materials 兜底: load_template 载入的草稿 (服务重启后 warmup 的) 素材不在
        # script.materials 而在 imported_materials (dict 形式) — 不补的话 save 会以为"无素材",
        # 重建出 0 assets 的空草稿目录, 渲染节点注入后素材丢失被拒导 (2026-08-24 实锤).
        _imported = getattr(script, 'imported_materials', {}) or {}
        for _mkey, _type_dir in (('videos', 'video'), ('audios', 'audio')):
            for _m in (_imported.get(_mkey) or []):
                if not isinstance(_m, dict):
                    continue
                _mname = _m.get('material_name') or _m.get('name')
                if not _mname:
                    continue
                _remote = (_m.get('remote_url') or '').strip()
                _local = (_m.get('path') or '').replace('\\', '/')
                _dir = 'image' if (_m.get('material_type') == 'photo' or _m.get('type') == 'photo') else _type_dir
                _dst = os.path.join(output_base_dir, f"{draft_id}/assets/{_dir}/{_mname}")
                _already = any(t['args'][1] == _dst for t in download_tasks) or os.path.isfile(_dst)
                if _already:
                    continue
                if _remote:
                    download_tasks.append({
                        'type': _dir,
                        'func': download_file,
                        'args': (_remote, _dst),
                        'material': None
                    })
                elif _local and os.path.isfile(_local):
                    os.makedirs(os.path.dirname(_dst), exist_ok=True)
                    shutil.copyfile(_local, _dst)
                    logger.info(f"imported material {_mname} copied from local path.")
                else:
                    failed_downloads.append({
                        'type': _dir, 'material_name': _mname,
                        'remote_url': _remote, 'error': f'无 remote_url 且本地文件不存在: {_local!r}'})

        update_task_field(task_id, "message", f"Collected {len(download_tasks)} download tasks in total")
        update_task_field(task_id, "progress", 10)
        logger.info(f"Task {task_id} progress 10%: Collected {len(download_tasks)} download tasks in total.")

        # Execute all download tasks concurrently
        downloaded_paths = []
        completed_files = 0
        if download_tasks:
            logger.info(f"Starting concurrent download of {len(download_tasks)} files...")
            
            # Use thread pool for concurrent downloads, maximum concurrency of 16
            with ThreadPoolExecutor(max_workers=16) as executor:
                # Submit all download tasks
                future_to_task = {
                    executor.submit(task['func'], *task['args']): task 
                    for task in download_tasks
                }
                
                # Wait for all tasks to complete
                for future in as_completed(future_to_task):
                    task = future_to_task[future]
                    try:
                        local_path = future.result()
                        downloaded_paths.append(local_path)
                        
                        # Update task status - only update completed files count
                        completed_files += 1
                        update_task_field(task_id, "completed_files", completed_files)
                        task_status = get_task_status(task_id)
                        completed = task_status["completed_files"]
                        total = len(download_tasks)
                        update_task_field(task_id, "total_files", total)
                        # Download part accounts for 60% of the total progress
                        download_progress = 10 + int((completed / total) * 60)
                        update_task_field(task_id, "progress", download_progress)
                        update_task_field(task_id, "message", f"Downloaded {completed}/{total} files")
                        
                        logger.info(f"Task {task_id}: Successfully downloaded {task['type']} file, progress {download_progress}.")
                    except Exception as e:
                        logger.error(f"Task {task_id}: Download {task['type']} file failed: {str(e)}", exc_info=True)
                        # Continue processing other files, don't interrupt the entire process
                        # (但记入失败清单, 全部下载完后统一判失败 — 见下方)
                        failed_downloads.append({
                            'type': task['type'],
                            'material_name': getattr(task.get('material'), 'material_name', '?'),
                            'remote_url': getattr(task.get('material'), 'remote_url', '?'),
                            'error': str(e)
                        })

            logger.info(f"Task {task_id}: Concurrent download completed, downloaded {len(downloaded_paths)} files in total.")

            if failed_downloads:
                # 缺料的草稿包发到渲染端, 剪映会因素材丢失拒绝导出(表现为"点导出报错"),
                # 渲染端只能报错回退 — 与其发不完整的包, 不如在这里直接判任务失败,
                # 列明缺哪些素材, 用户重新上传即可解决.
                names = ', '.join(sorted({f["material_name"] for f in failed_downloads}))
                update_tasks_cache(task_id, {
                    "status": "failed",
                    "message": f"{len(failed_downloads)} material(s) failed to download: {names}",
                    "progress": 100,
                    "completed_files": completed_files,
                    "total_files": len(download_tasks),
                    "draft_url": ""
                })
                logger.error(f"Task {task_id} FAILED: {len(failed_downloads)} material(s) missing, abort before zip: "
                             f"{failed_downloads}")
                return
        
        # Update task status - Start saving draft information
        update_task_field(task_id, "progress", 70)
        update_task_field(task_id, "message", "Saving draft information")
        logger.info(f"Task {task_id} progress 70%: Saving draft information.")
        
        # 导出前清洗: 悬挂引用 + 重复素材 (load_template 往返的副产品, 不清洗会出毒草稿)
        _sanitize_script_for_export(script)

        written_files = write_profile_content(draft_profile, draft_dir, script.dumps(draft_profile))
        logger.info(f"Draft information has been saved to {[str(path) for path in written_files]}.")

        if _dl is not None:
            try: _dl.release()
            except Exception: pass
        draft_url = ""
        # Only upload draft information when IS_UPLOAD_DRAFT is True
        if IS_UPLOAD_DRAFT:
            # Update task status - Start compressing draft
            update_task_field(task_id, "progress", 80)
            update_task_field(task_id, "message", "Compressing draft files")
            logger.info(f"Task {task_id} progress 80%: Compressing draft files.")
            
            # Compress the entire draft directory
            zip_path = zip_draft(draft_id)
            logger.info(f"Draft directory {os.path.join(current_dir, draft_id)} has been compressed to {zip_path}.")
            
            # Update task status - Start uploading to OSS
            update_task_field(task_id, "progress", 90)
            update_task_field(task_id, "message", "Uploading to cloud storage")
            logger.info(f"Task {task_id} progress 90%: Uploading to cloud storage.")
            
            # Upload to OSS
            draft_url = upload_to_oss(zip_path)
            logger.info(f"Draft archive has been uploaded to OSS, URL: {draft_url}")
            update_task_field(task_id, "draft_url", draft_url)

            # Clean up temporary files
            if os.path.exists(os.path.join(current_dir, draft_id)):
                shutil.rmtree(os.path.join(current_dir, draft_id))
                logger.info(f"Cleaned up temporary draft folder: {os.path.join(current_dir, draft_id)}")

    
        # Update task status - Completed
        update_task_field(task_id, "status", "completed")
        update_task_field(task_id, "progress", 100)
        update_task_field(task_id, "message", "Draft creation completed")
        logger.info(f"Task {task_id} completed, draft URL: {draft_url}")
        return draft_url

    except Exception as e:
        # Update task status - Failed
        update_task_fields(task_id,
                          status="failed",
                          message=f"Failed to save draft: {str(e)}")
        logger.error(f"Saving draft {draft_id} task {task_id} failed: {str(e)}", exc_info=True)
        # 异常路径也必须释放草稿锁 (否则该草稿的后续编辑永久阻塞)
        try:
            if '_dl' in dir() and _dl is not None:
                _dl.release()
        except Exception:
            pass
        return ""

def query_task_status(task_id: str):
    return get_task_status(task_id)

def save_draft_impl(draft_id: str, draft_folder: str = None) -> Dict[str, str]:
    """Start a background task to save the draft"""
    logger.info(f"Received save draft request: draft_id={draft_id}, draft_folder={draft_folder}")
    try:
        # Generate a unique task ID
        task_id = draft_id
        create_task(task_id)
        logger.info(f"Task {task_id} has been created.")
        
        # Changed to synchronous execution
        return {
            "success": True,
            "draft_url": save_draft_background(draft_id, draft_folder, task_id)
            }

        # # Start a background thread to execute the task
        # thread = threading.Thread(
        #     target=save_draft_background,
        #     args=(draft_id, draft_folder, task_id)
        # )
        # thread.start()
        
    except Exception as e:
        logger.error(f"Failed to start save draft task {draft_id}: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }

def update_media_metadata(script, task_id=None):
    """
    Update metadata for all media files in the script (duration, width/height, etc.)
    
    :param script: Draft script object
    :param task_id: Optional task ID for updating task status
    :return: None
    """
    # Process audio file metadata
    audios = script.materials.audios
    if not audios:
        logger.info("No audio files found in the draft.")
    else:
        for audio in audios:
            remote_url = audio.remote_url
            material_name = audio.material_name
            if not remote_url:
                logger.warning(f"Warning: Audio file {material_name} has no remote_url, skipped.")
                continue
            
            try:
                video_command = [
                    'ffprobe',
                    '-v', 'error',
                    '-select_streams', 'v:0',
                    '-show_entries', 'stream=codec_type',
                    '-of', 'json',
                    remote_url
                ]
                video_result = subprocess.check_output(video_command, stderr=subprocess.STDOUT)
                video_result_str = video_result.decode('utf-8')
                # Find JSON start position (first '{')
                video_json_start = video_result_str.find('{')
                if video_json_start != -1:
                    video_json_str = video_result_str[video_json_start:]
                    video_info = json.loads(video_json_str)
                    if 'streams' in video_info and len(video_info['streams']) > 0:
                        logger.warning(f"Warning: Audio file {material_name} contains video tracks, skipped its metadata update.")
                        continue
            except Exception as e:
                logger.error(f"Error occurred while checking if audio {material_name} contains video streams: {str(e)}", exc_info=True)

            # Get audio duration and set it
            try:
                duration_result = get_video_duration(remote_url)
                if duration_result["success"]:
                    if task_id:
                        update_task_field(task_id, "message", f"Processing audio metadata: {material_name}")
                    # Convert seconds to microseconds
                    audio.duration = int(duration_result["output"] * 1000000)
                    logger.info(f"Successfully obtained audio {material_name} duration: {duration_result['output']:.2f} seconds ({audio.duration} microseconds).")
                    
                    # Update timerange for all segments using this audio material
                    for track_name, track in script.tracks.items():
                        if track.track_type == draft.Track_type.audio:
                            for segment in track.segments:
                                if isinstance(segment, draft.Audio_segment) and segment.material_id == audio.material_id:
                                    # Get current settings
                                    current_target = segment.target_timerange
                                    current_source = segment.source_timerange
                                    speed = segment.speed.speed
                                    
                                    # If the end time of source_timerange exceeds the new audio duration, adjust it
                                    if current_source.end > audio.duration or current_source.end <= 0:
                                        # Adjust source_timerange to fit the new audio duration
                                        new_source_duration = audio.duration - current_source.start
                                        if new_source_duration <= 0:
                                            logger.warning(f"Warning: Audio segment {segment.segment_id} start time {current_source.start} exceeds audio duration {audio.duration}, will skip this segment.")
                                            continue
                                            
                                        # Update source_timerange
                                        segment.source_timerange = draft.Timerange(current_source.start, new_source_duration)
                                        
                                        # Update target_timerange based on new source_timerange and speed
                                        new_target_duration = int(new_source_duration / speed)
                                        segment.target_timerange = draft.Timerange(current_target.start, new_target_duration)
                                        
                                        logger.info(f"Adjusted audio segment {segment.segment_id} timerange to fit the new audio duration.")
                else:
                    logger.warning(f"Warning: Unable to get audio {material_name} duration: {duration_result['error']}.")
            except Exception as e:
                logger.error(f"Error occurred while getting audio {material_name} duration: {str(e)}", exc_info=True)
    
    # Process video and image file metadata
    videos = script.materials.videos
    if not videos:
        logger.info("No video or image files found in the draft.")
    else:
        for video in videos:
            remote_url = video.remote_url
            material_name = video.material_name
            if not remote_url:
                logger.warning(f"Warning: Media file {material_name} has no remote_url, skipped.")
                continue
                
            if video.material_type == 'photo':
                # Use imageio to get image width/height and set it
                try:
                    if task_id:
                        update_task_field(task_id, "message", f"Processing image metadata: {material_name}")
                    img = imageio.imread(remote_url)
                    video.height, video.width = img.shape[:2]
                    logger.info(f"Successfully set image {material_name} dimensions: {video.width}x{video.height}.")
                except Exception as e:
                    logger.error(f"Failed to set image {material_name} dimensions: {str(e)}, using default values 1920x1080.", exc_info=True)
                    video.width = 1920
                    video.height = 1080
            
            elif video.material_type == 'video':
                # Get video duration and width/height information
                try:
                    if task_id:
                        update_task_field(task_id, "message", f"Processing video metadata: {material_name}")
                    # Use ffprobe to get video information
                    command = [
                        'ffprobe',
                        '-v', 'error',
                        '-select_streams', 'v:0',  # Select the first video stream
                        '-show_entries', 'stream=width,height,duration',
                        '-show_entries', 'format=duration',
                        '-of', 'json',
                        remote_url
                    ]
                    result = subprocess.check_output(command, stderr=subprocess.STDOUT)
                    result_str = result.decode('utf-8')
                    # Find JSON start position (first '{')
                    json_start = result_str.find('{')
                    if json_start != -1:
                        json_str = result_str[json_start:]
                        info = json.loads(json_str)
                        
                        if 'streams' in info and len(info['streams']) > 0:
                            stream = info['streams'][0]
                            # Set width and height
                            video.width = int(stream.get('width', 0))
                            video.height = int(stream.get('height', 0))
                            logger.info(f"Successfully set video {material_name} dimensions: {video.width}x{video.height}.")
                            
                            # Set duration
                            # Prefer stream duration, if not available use format duration
                            duration = stream.get('duration') or info['format'].get('duration', '0')
                            video.duration = int(float(duration) * 1000000)  # Convert to microseconds
                            logger.info(f"Successfully obtained video {material_name} duration: {float(duration):.2f} seconds ({video.duration} microseconds).")
                            
                            # Update timerange for all segments using this video material
                            for track_name, track in script.tracks.items():
                                if track.track_type == draft.Track_type.video:
                                    for segment in track.segments:
                                        if isinstance(segment, draft.Video_segment) and segment.material_id == video.material_id:
                                            # Get current settings
                                            current_target = segment.target_timerange
                                            current_source = segment.source_timerange
                                            speed = segment.speed.speed

                                            # If the end time of source_timerange exceeds the new video duration, adjust it
                                            if current_source.end > video.duration or current_source.end <= 0:
                                                # Adjust source_timerange to fit the new video duration
                                                new_source_duration = video.duration - current_source.start
                                                if new_source_duration <= 0:
                                                    logger.warning(f"Warning: Video segment {segment.segment_id} start time {current_source.start} exceeds video duration {video.duration}, will skip this segment.")
                                                    continue
                                                    
                                                # Update source_timerange
                                                segment.source_timerange = draft.Timerange(current_source.start, new_source_duration)
                                                
                                                # Update target_timerange based on new source_timerange and speed
                                                new_target_duration = int(new_source_duration / speed)
                                                segment.target_timerange = draft.Timerange(current_target.start, new_target_duration)
                                                
                                                logger.info(f"Adjusted video segment {segment.segment_id} timerange to fit the new video duration.")
                        else:
                            logger.warning(f"Warning: Unable to get video {material_name} stream information.")
                            # Set default values
                            video.width = 1920
                            video.height = 1080
                    else:
                        logger.warning(f"Warning: Could not find JSON data in ffprobe output.")
                        # Set default values
                        video.width = 1920
                        video.height = 1080
                except Exception as e:
                    logger.error(f"Error occurred while getting video {material_name} information: {str(e)}, using default values 1920x1080.", exc_info=True)
                    # Set default values
                    video.width = 1920
                    video.height = 1080
                    
                    # Try to get duration separately
                    try:
                        duration_result = get_video_duration(remote_url)
                        if duration_result["success"]:
                            # Convert seconds to microseconds
                            video.duration = int(duration_result["output"] * 1000000)
                            logger.info(f"Successfully obtained video {material_name} duration: {duration_result['output']:.2f} seconds ({video.duration} microseconds).")
                        else:
                            logger.warning(f"Warning: Unable to get video {material_name} duration: {duration_result['error']}.")
                    except Exception as e2:
                        logger.error(f"Error occurred while getting video {material_name} duration: {str(e2)}.", exc_info=True)

    # After updating all segments' timerange, check if there are time range conflicts in each track, and delete the later segment in case of conflict
    logger.info("Checking track segment time range conflicts...")
    for track_name, track in script.tracks.items():
        # Use a set to record segment indices that need to be deleted
        to_remove = set()
        
        # Check for conflicts between all segments
        for i in range(len(track.segments)):
            # Skip if current segment is already marked for deletion
            if i in to_remove:
                continue
                
            for j in range(len(track.segments)):
                # Skip self-comparison and segments already marked for deletion
                if i == j or j in to_remove:
                    continue
                    
                # Check if there is a conflict
                if track.segments[i].overlaps(track.segments[j]):
                    # Always keep the segment with the smaller index (added first)
                    later_index = max(i, j)
                    logger.warning(f"Time range conflict between segments {track.segments[min(i, j)].segment_id} and {track.segments[later_index].segment_id} in track {track_name}, deleting the later segment")
                    to_remove.add(later_index)
        
        # Delete marked segments from back to front to avoid index change issues
        for index in sorted(to_remove, reverse=True):
            track.segments.pop(index)

    # After updating all segments' timerange, recalculate the total duration of the script
    max_duration = 0
    for track_name, track in script.tracks.items():
        for segment in track.segments:
            max_duration = max(max_duration, segment.end)
    # imported_tracks 也算 (load_template 载入的草稿轨道全在这, 不算会重算成 0 毁掉时长)
    for track in getattr(script, 'imported_tracks', []) or []:
        for segment in track.segments:
            try:
                max_duration = max(max_duration, segment.end)
            except Exception:
                continue
    script.duration = max_duration
    logger.info(f"Updated script total duration to: {script.duration} microseconds.")
    
    # Process all pending keyframes in tracks
    logger.info("Processing pending keyframes...")
    for track_name, track in script.tracks.items():
        if hasattr(track, 'pending_keyframes') and track.pending_keyframes:
            logger.info(f"Processing {len(track.pending_keyframes)} pending keyframes in track {track_name}...")
            track.process_pending_keyframes()
            logger.info(f"Pending keyframes in track {track_name} have been processed.")

def query_script_impl(draft_id: str, force_update: bool = True):
    """
    Query draft script object, with option to force refresh media metadata
    
    :param draft_id: Draft ID
    :param force_update: Whether to force refresh media metadata, default is True
    :return: Script object
    """
    # Get draft information from global cache
    if draft_id not in DRAFT_CACHE:
        logger.warning(f"Draft {draft_id} does not exist in cache.")
        return None
        
    script = DRAFT_CACHE[draft_id]
    logger.info(f"Retrieved draft {draft_id} from cache.")
    
    # If force_update is True, force refresh media metadata
    if force_update:
        logger.info(f"Force refreshing media metadata for draft {draft_id}.")
        update_media_metadata(script)
    
    # Return script object
    return script

def download_script(draft_id: str, draft_folder: str = None, script_data: Dict = None) -> Dict[str, str]:
    """Downloads the draft script and its associated media assets.

    This function fetches the script object from a remote API,
    then iterates through its materials (audios, videos, images)
    to download them to the specified draft folder. It also updates
    task status and progress throughout the process.

    :param draft_id: The ID of the draft to download.
    :param draft_folder: The base folder where the draft's assets will be stored.
                         If None, assets will be stored directly under a folder named
                         after the draft_id in the current working directory.
    :return: A dictionary indicating success and, if successful, the URL where the draft
             would eventually be saved (though this function primarily focuses on download).
             If failed, it returns an error message.
    """

    logger.info(f"Starting to download draft: {draft_id} to folder: {draft_folder}")
    # Copy template to target directory
    draft_profile = get_draft_profile()
    template_path = os.path.join("./", draft_profile.template_dir)
    new_draft_path = os.path.join(draft_folder, draft_id)
    if os.path.exists(new_draft_path):
        logger.warning(f"Deleting existing draft target folder: {new_draft_path}")
        shutil.rmtree(new_draft_path)

    # Copy draft folder
    shutil.copytree(template_path, new_draft_path)
    
    try:
        # 1. Fetch the script from the remote endpoint
        if script_data is None:
            query_url = "https://cut-jianying-vdvswivepm.cn-hongkong.fcapp.run/query_script"
            headers = {"Content-Type": "application/json"}
            payload = {"draft_id": draft_id}

            logger.info(f"Attempting to get script for draft ID: {draft_id} from {query_url}.")
            response = requests.post(query_url, headers=headers, json=payload)
            response.raise_for_status()  # Raise an exception for HTTP errors (4xx or 5xx)
            
            script_data = json.loads(response.json().get('output'))
            logger.info(f"Successfully retrieved script data for draft {draft_id}.")
        else:
            logger.info(f"Using provided script_data, skipping remote retrieval.")

        # Collect download tasks
        download_tasks = []
        
        # Collect audio download tasks
        audios = script_data.get('materials',{}).get('audios',[])
        if audios:
            for audio in audios:
                remote_url = audio['remote_url']
                material_name = audio['name']
                # Use helper function to build path
                if draft_folder:
                    audio['path']=build_asset_path(draft_folder, draft_id, "audio", material_name)
                    logger.debug(f"Local path for audio {material_name}: {audio['path']}")
                if not remote_url:
                    local_p = getattr(audio, 'path', '') or ''
                    dst = os.path.join(output_base_dir, f"{draft_id}/assets/audio/{material_name}")
                    if local_p and os.path.isfile(local_p.replace('\\', '/')):
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copyfile(local_p.replace('\\', '/'), dst)
                        logger.info(f"Audio file {material_name} copied from local path.")
                    else:
                        failed_downloads.append({
                            'type': 'audio', 'material_name': material_name,
                            'remote_url': '', 'error': f'无 remote_url 且本地文件不存在: {local_p!r}'})
                    continue
                
                # Add audio download task
                download_tasks.append({
                    'type': 'audio',
                    'func': download_file,
                    'args': (remote_url, audio['path']),
                    'material': audio
                })
        
        # Collect video and image download tasks
        videos = script_data['materials']['videos']
        if videos:
            for video in videos:
                remote_url = video['remote_url']
                material_name = video['material_name']
                
                if video['type'] == 'photo':
                    # Use helper function to build path
                    if draft_folder:
                        video['path'] = build_asset_path(draft_folder, draft_id, "image", material_name)
                    if not remote_url:
                        # 本地素材: 从 .path 就地拷贝进 assets (历史行为是跳过,
                        # 导致重建的草稿目录无 assets, 渲染节点注入后素材丢失被拒导).
                        local_p = getattr(video, 'path', '') or ''
                        dst = os.path.join(output_base_dir, f"{draft_id}/assets/image/{material_name}")
                        if local_p and os.path.isfile(local_p.replace('\\', '/')):
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            shutil.copyfile(local_p.replace('\\', '/'), dst)
                            logger.info(f"Image file {material_name} copied from local path.")
                        else:
                            failed_downloads.append({
                                'type': 'image', 'material_name': material_name,
                                'remote_url': '', 'error': f'无 remote_url 且本地文件不存在: {local_p!r}'})
                        continue
                    
                    # Add image download task
                    download_tasks.append({
                        'type': 'image',
                        'func': download_file,
                        'args': (remote_url, video['path']),
                        'material': video
                    })
                
                elif video['type'] == 'video':
                    # Use helper function to build path
                    if draft_folder:
                        video['path'] = build_asset_path(draft_folder, draft_id, "video", material_name)
                    if not remote_url:
                        # 本地素材: 从 .path 就地拷贝进 assets (历史行为是跳过,
                        # 导致重建的草稿目录无 assets, 渲染节点注入后素材丢失被拒导).
                        local_p = getattr(video, 'path', '') or ''
                        dst = os.path.join(output_base_dir, f"{draft_id}/assets/video/{material_name}")
                        if local_p and os.path.isfile(local_p.replace('\\', '/')):
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            shutil.copyfile(local_p.replace('\\', '/'), dst)
                            logger.info(f"Video file {material_name} copied from local path.")
                        else:
                            failed_downloads.append({
                                'type': 'video', 'material_name': material_name,
                                'remote_url': '', 'error': f'无 remote_url 且本地文件不存在: {local_p!r}'})
                        continue
                    
                    # Add video download task
                    download_tasks.append({
                        'type': 'video',
                        'func': download_file,
                        'args': (remote_url, video['path']),
                        'material': video
                    })

        # Execute all download tasks concurrently
        downloaded_paths = []
        completed_files = 0
        failed_downloads = []  # 下载失败清单 — 缺料草稿在剪映端无法渲染, 必须拦截
        if download_tasks:
            logger.info(f"Starting concurrent download of {len(download_tasks)} files...")
            
            # Use thread pool for concurrent downloads, maximum concurrency of 16
            with ThreadPoolExecutor(max_workers=16) as executor:
                # Submit all download tasks
                future_to_task = {
                    executor.submit(task['func'], *task['args']): task 
                    for task in download_tasks
                }
                
                # Wait for all tasks to complete
                for future in as_completed(future_to_task):
                    task = future_to_task[future]
                    try:
                        local_path = future.result()
                        downloaded_paths.append(local_path)
                        
                        # Update task status - only update completed files count
                        completed_files += 1
                        logger.info(f"Downloaded {completed_files}/{len(download_tasks)} files.")
                    except Exception as e:
                        logger.error(f"Failed to download {task['type']} file {task['args'][0]}: {str(e)}", exc_info=True)
                        logger.error("Download failed.")
                        # Continue processing other files, don't interrupt the entire process
                        # (但记入失败清单, 全部下载完后统一报错 — 见下方)
                        _mat = task.get('material') or {}
                        failed_downloads.append({
                            'type': task['type'],
                            'material_name': (_mat.get('material_name') if isinstance(_mat, dict)
                                              else getattr(_mat, 'material_name', '?')),
                            'remote_url': task['args'][0],
                            'error': str(e)
                        })

            logger.info(f"Concurrent download completed, downloaded {len(downloaded_paths)} files in total.")

        if failed_downloads:
            # 缺料的草稿包发到渲染端, 剪映会因素材丢失拒绝导出 — 直接返回失败并列明缺哪些素材,
            # 与 save_draft_background 的拦截逻辑同源.
            names = ', '.join(sorted({str(f['material_name']) for f in failed_downloads}))
            logger.error(f"{len(failed_downloads)} material(s) missing, abort before writing draft: {failed_downloads}")
            return {"success": False,
                    "error": f"{len(failed_downloads)} material(s) failed to download: {names}"}
        
        """Write draft file content to file"""
        write_profile_content(draft_profile, os.path.join(draft_folder, draft_id), json.dumps(script_data, ensure_ascii=False))
        logger.info(f"Draft has been saved.")

        # No draft_url for download, but return success
        return {"success": True, "message": f"Draft {draft_id} and its assets downloaded successfully"}

    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}", exc_info=True)
        return {"success": False, "error": f"Failed to fetch script from API: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error during download: {e}", exc_info=True)
        return {"success": False, "error": f"An unexpected error occurred: {str(e)}"}

if __name__ == "__main__":
    print('hello')
    download_script("dfd_cat_1751012163_a7e8c315",'/Users/sunguannan/Movies/JianyingPro/User Data/Projects/com.lveditor.draft')
