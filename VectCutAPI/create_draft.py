import uuid
import os
import pyJianYingDraft as draft
import time
from draft_cache import DRAFT_CACHE, update_cache


def _user_prefix(user_id):
    """多租户: draft_id 加 user 前缀 (如 u<uid8>_), 既让 DRAFT_CACHE 全局唯一,
    又能在按草稿目录列举/访问时按前缀做租户隔离。user_id=None 不加前缀 (兼容 legacy)。
    取 user_id 前 8 位 hex (与 uid 生成处一致), 足够区分且短。"""
    if not user_id:
        return ''
    uid = str(user_id)
    return f'u{uid[:8]}_'


def create_draft(width=1080, height=1920, user_id=None):
    """
    Create new CapCut draft
    :param width: Video width, default 1080
    :param height: Video height, default 1920
    :param user_id: 可选, 多租户归属。给定时 draft_id 加 user 前缀 (u<uid8>_)。
    :return: (draft_name, draft_path, draft_id, draft_url)
    """
    # Generate timestamp and draft_id
    unix_time = int(time.time())
    unique_id = uuid.uuid4().hex[:8]  # Take the first 8 digits of UUID
    prefix = _user_prefix(user_id)
    draft_id = f"{prefix}dfd_cat_{unix_time}_{unique_id}"  # 多租户: u<uid8>_dfd_cat_<unix>_<uuid8>

    # Create CapCut draft with specified resolution
    script = draft.Script_file(width, height)

    # Store in global cache
    update_cache(draft_id, script)

    # 立即落盘 draft_content.json (即使还是空草稿). 不落盘的话:
    # /api/drafts 列表 (按 draft_content.json 扫描) 看不到新建的空草稿, 用户无法在
    # Drafts 面板选中/激活它; 服务重启后空草稿直接消失. 完整保存仍由 save_draft 做.
    try:
        vc_dir = os.path.dirname(os.path.abspath(__file__))
        ddir = os.path.join(vc_dir, draft_id)
        os.makedirs(ddir, exist_ok=True)
        with open(os.path.join(ddir, 'draft_content.json'), 'w', encoding='utf-8') as f:
            f.write(script.dumps())
    except Exception as e:
        print(f'[create_draft] 空草稿预落盘失败 (不影响缓存使用, save_draft 时会完整保存): {e}', flush=True)

    return script, draft_id


def get_or_create_draft(draft_id=None, width=1080, height=1920, user_id=None):
    """
    Get or create CapCut draft
    :param draft_id: Draft ID, if None or corresponding zip file not found, create new draft
    :param width: Video width, default 1080
    :param height: Video height, default 1920
    :param user_id: 可选, 新建草稿时的多租户归属 (仅当新建时生效; 已存在的 draft_id 原样复用)。
    :return: (draft_name, draft_path, draft_id, draft_dir, script)
    """
    global DRAFT_CACHE  # Declare use of global variable

    if draft_id is not None and draft_id in DRAFT_CACHE:
        # Get existing draft information from cache
        print(f"Getting draft from cache: {draft_id}")
        # Update last access time
        update_cache(draft_id, DRAFT_CACHE[draft_id])
        return draft_id, DRAFT_CACHE[draft_id]

    # Create new draft logic
    print("Creating new draft")
    script, generate_draft_id = create_draft(
        width=width,
        height=height,
        user_id=user_id,
    )
    return generate_draft_id, script
    