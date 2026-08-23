# chat_store.py — AI Assistant 对话历史 SQLite 持久化 (stdlib sqlite3)
# 模式与 task_store.py 一致: 整条记录序列化成 JSON 存一列, 避免关系型字段映射麻烦.
# messages 存的是前端展示用的完整结构 [{role, content, toolDetails?}, ...] —
# 多轮记忆时只取里面 role in (user, assistant) 且 content 非空的条目喂给 LLM,
# tool 卡片只是给用户看的执行细节, 不需要还原成 OpenAI 的 tool_call_id 链路.
import os
import sqlite3
import threading
import json
import time
import uuid

import config

DB_PATH = os.environ.get('CHAT_DB', os.path.join(config.HERE, 'chats.db'))

_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock:
        c = _conn()
        try:
            c.execute('''CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT,
                draft_id TEXT,
                messages TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )''')
            # 多租户迁移: 补 user_id 列 (单租户历史行 user_id 留 NULL, 视为 legacy 全局可见)
            cols = {r['name'] for r in c.execute('PRAGMA table_info(conversations)').fetchall()}
            if 'user_id' not in cols:
                c.execute('ALTER TABLE conversations ADD COLUMN user_id TEXT')
            c.commit()
        finally:
            c.close()


def create(draft_id=None, user_id=None):
    """新建一条空会话, 返回 id."""
    cid = uuid.uuid4().hex
    now = time.time()
    with _lock:
        c = _conn()
        try:
            c.execute('INSERT INTO conversations (id, title, draft_id, messages, created_at, updated_at, user_id) '
                      'VALUES (?, ?, ?, ?, ?, ?, ?)',
                      (cid, None, draft_id, json.dumps([]), now, now, user_id))
            c.commit()
        finally:
            c.close()
    return cid


def list_all(user_id=None):
    """按最近更新排序, 供侧栏历史列表用 (不含 messages 正文, 省流量).
    user_id 给定时只返回该用户的会话; legacy 行 (user_id NULL) 不归任何用户, 仅 user_id=None 时可见。"""
    out = []
    try:
        c = _conn()
        try:
            if user_id is not None:
                rows = c.execute(
                    'SELECT id, title, draft_id, created_at, updated_at, messages FROM conversations '
                    'WHERE user_id = ? ORDER BY updated_at DESC', (user_id,)
                )
            else:
                rows = c.execute(
                    'SELECT id, title, draft_id, created_at, updated_at, messages FROM conversations '
                    'ORDER BY updated_at DESC'
                )
            for row in rows:
                try:
                    msg_count = len(json.loads(row['messages']))
                except (ValueError, TypeError):
                    msg_count = 0
                out.append({
                    'id': row['id'], 'title': row['title'], 'draft_id': row['draft_id'],
                    'created_at': row['created_at'], 'updated_at': row['updated_at'],
                    'message_count': msg_count,
                })
        finally:
            c.close()
    except sqlite3.Error:
        pass
    return out


def list_by_draft(draft_id, user_id=None):
    """列出某草稿下的所有对话 (按更新时间倒序), 供 Chat 侧栏按草稿过滤展示.
    不含 messages 正文. 形状与 list_all() 一致, 便于前端复用同一渲染逻辑。
    user_id 给定时只返回该用户在该草稿下的会话。"""
    if not draft_id:
        return []
    out = []
    try:
        c = _conn()
        try:
            if user_id is not None:
                rows = c.execute(
                    'SELECT id, title, draft_id, created_at, updated_at, messages FROM conversations '
                    'WHERE draft_id = ? AND user_id = ? ORDER BY updated_at DESC',
                    (draft_id, user_id)
                )
            else:
                rows = c.execute(
                    'SELECT id, title, draft_id, created_at, updated_at, messages FROM conversations '
                    'WHERE draft_id = ? ORDER BY updated_at DESC',
                    (draft_id,)
                )
            for row in rows:
                try:
                    msg_count = len(json.loads(row['messages']))
                except (ValueError, TypeError):
                    msg_count = 0
                out.append({
                    'id': row['id'], 'title': row['title'], 'draft_id': row['draft_id'],
                    'created_at': row['created_at'], 'updated_at': row['updated_at'],
                    'message_count': msg_count,
                })
        finally:
            c.close()
    except sqlite3.Error:
        pass
    return out


def get(conversation_id, user_id=None):
    """取一条完整会话 (含 messages). 不存在返回 None.
    user_id 给定时: 会话 owner 不匹配 (且非 legacy NULL) 返回 None (租户隔离)。"""
    try:
        c = _conn()
        try:
            row = c.execute(
                'SELECT id, title, draft_id, messages, created_at, updated_at, user_id '
                'FROM conversations WHERE id = ?', (conversation_id,)
            ).fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    # 租户隔离: 该会话有明确 owner 且不匹配调用方 → 不返回
    if user_id is not None and row['user_id'] is not None and row['user_id'] != user_id:
        return None
    try:
        messages = json.loads(row['messages'])
    except (ValueError, TypeError):
        messages = []
    return {
        'id': row['id'], 'title': row['title'], 'draft_id': row['draft_id'],
        'messages': messages, 'created_at': row['created_at'], 'updated_at': row['updated_at'],
    }


def _auto_title(messages):
    """没标题时, 从第一条用户消息截断生成一个."""
    for m in messages:
        if m.get('role') == 'user' and m.get('content'):
            t = m['content'].strip().replace('\n', ' ')
            return (t[:24] + '…') if len(t) > 24 else t
    return None


def save_messages(conversation_id, messages, draft_id=None, user_id=None):
    """覆盖保存整个会话的 messages (+可选更新 draft_id), 首次有内容时自动生成标题.
    conversation_id 不存在时静默新建 (兜底: 前端理论上总是先 create 再用).
    user_id: 新建会话时写入 owner; 已存在会话不改变 owner (保留创建时归属)。"""
    now = time.time()
    with _lock:
        c = _conn()
        try:
            row = c.execute('SELECT title, user_id FROM conversations WHERE id = ?',
                            (conversation_id,)).fetchone()
            title = row['title'] if row else None
            if not title:
                title = _auto_title(messages)
            payload = json.dumps(messages, ensure_ascii=False)
            if row:
                if draft_id is not None:
                    c.execute('UPDATE conversations SET title=?, draft_id=?, messages=?, updated_at=? WHERE id=?',
                             (title, draft_id, payload, now, conversation_id))
                else:
                    c.execute('UPDATE conversations SET title=?, messages=?, updated_at=? WHERE id=?',
                             (title, payload, now, conversation_id))
            else:
                c.execute('INSERT INTO conversations (id, title, draft_id, messages, created_at, updated_at, user_id) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (conversation_id, title, draft_id, payload, now, now, user_id))
            c.commit()
        finally:
            c.close()


def delete(conversation_id, user_id=None):
    """删除会话。user_id 给定时: 仅删除 owner 匹配 (或 legacy NULL) 的会话, 返回是否删除成功。"""
    with _lock:
        c = _conn()
        try:
            if user_id is not None:
                cur = c.execute(
                    'DELETE FROM conversations WHERE id = ? AND (user_id = ? OR user_id IS NULL)',
                    (conversation_id, user_id)
                )
            else:
                cur = c.execute('DELETE FROM conversations WHERE id = ?', (conversation_id,))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()


# 导入时初始化表
try:
    init()
except Exception:
    pass
