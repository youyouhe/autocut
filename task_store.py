# task_store.py — 渲染任务 SQLite 持久化 (stdlib sqlite3)
# 让 render_server 重启后任务历史/结果仍可查询 (tasks 内存 dict 的落盘层)
import os
import sqlite3
import threading
import json

import config

DB_PATH = os.environ.get('TASK_DB', os.path.join(config.HERE, 'tasks.db'))

_lock = threading.Lock()
# 序列化字段 (JSON 存储, 避免 SQL 字段类型映射麻烦)
# render_url/render_token/fallback_reason: per-user render 节点路由记录 ——
# 提交成功时记下用了哪个节点, 重启后 _sync_remote_status 才能打对端点拉状态/下载.
# 缺这三项会导致重启后丢失节点路由 (全回退公共, 拉错节点的 remote_task_id → 404).
_KEEP_KEYS = ('task_id', 'status', 'draft_name', 'draft_dir', 'created',
              'started_at', 'duration', 'mp4_path', 'mp4_name', 'error', 'progress',
              'user_id', 'remote_task_id', 'render_url', 'render_token', 'fallback_reason')


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock:
        c = _conn()
        try:
            c.execute('''CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL
            )''')
            # 多租户迁移: 补 user_id 独立列 (便于按租户查询)。payload 内同步保留 user_id。
            cols = {r['name'] for r in c.execute('PRAGMA table_info(tasks)').fetchall()}
            if 'user_id' not in cols:
                c.execute('ALTER TABLE tasks ADD COLUMN user_id TEXT')
            c.execute('CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id)')
            c.commit()
        finally:
            c.close()


def upsert(task):
    """写入/更新一条任务 (dict). user_id 同时落独立列 (便于租户查询)。"""
    data = {k: task.get(k) for k in _KEEP_KEYS}
    user_id = task.get('user_id')
    with _lock:
        c = _conn()
        try:
            c.execute('INSERT OR REPLACE INTO tasks (task_id, payload, updated_at, user_id) '
                      'VALUES (?, ?, ?, ?)',
                      (task['task_id'], json.dumps(data, ensure_ascii=False),
                       __import__('time').time(), user_id))
            c.commit()
        finally:
            c.close()


def load_all(user_id=None):
    """加载任务, 返回 {task_id: dict}.
    user_id 给定时: 仅加载 owner 匹配 (或 legacy NULL) 的任务。"""
    out = {}
    try:
        c = _conn()
        try:
            if user_id is not None:
                rows = c.execute(
                    'SELECT task_id, payload FROM tasks WHERE (user_id=? OR user_id IS NULL)',
                    (user_id,)
                )
            else:
                rows = c.execute('SELECT task_id, payload FROM tasks')
            for row in rows:
                try:
                    out[row['task_id']] = json.loads(row['payload'])
                except (ValueError, TypeError):
                    continue
        finally:
            c.close()
    except sqlite3.Error:
        pass
    return out


def get(task_id, user_id=None):
    """取单条任务。user_id 给定时: owner 不匹配 (且非 legacy NULL) 返回 None。"""
    try:
        c = _conn()
        try:
            if user_id is not None:
                row = c.execute(
                    'SELECT payload FROM tasks WHERE task_id=? AND (user_id=? OR user_id IS NULL)',
                    (task_id, user_id)
                ).fetchone()
            else:
                row = c.execute('SELECT payload FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        return json.loads(row['payload'])
    except (ValueError, TypeError):
        return None


def delete(task_id):
    with _lock:
        c = _conn()
        try:
            c.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))
            c.commit()
        finally:
            c.close()


# 导入时初始化表
try:
    init()
except Exception:
    pass
