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
_KEEP_KEYS = ('task_id', 'status', 'draft_name', 'draft_dir', 'created',
              'started_at', 'duration', 'mp4_path', 'mp4_name', 'error', 'progress')


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
            c.commit()
        finally:
            c.close()


def upsert(task):
    """写入/更新一条任务 (dict)."""
    data = {k: task.get(k) for k in _KEEP_KEYS}
    with _lock:
        c = _conn()
        try:
            c.execute('INSERT OR REPLACE INTO tasks (task_id, payload, updated_at) '
                      'VALUES (?, ?, ?)',
                      (task['task_id'], json.dumps(data, ensure_ascii=False), __import__('time').time()))
            c.commit()
        finally:
            c.close()


def load_all():
    """加载所有任务, 返回 {task_id: dict}."""
    out = {}
    try:
        c = _conn()
        try:
            for row in c.execute('SELECT task_id, payload FROM tasks'):
                try:
                    out[row['task_id']] = json.loads(row['payload'])
                except (ValueError, TypeError):
                    continue
        finally:
            c.close()
    except sqlite3.Error:
        pass
    return out


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
