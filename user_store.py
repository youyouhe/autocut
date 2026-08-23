# user_store.py — 用户管理 SQLite 持久化 (admin 统一管理, 无注册)
#
# SaaS 多租户: 用户由 admin 创建/删除, 不开放自助注册。
# 密码用 werkzeug.security (随 Flask 自带, 零新依赖) 做哈希加盐, 不存明文。
# 启动时若无任何 admin, 自动 bootstrap 第一个 admin (env 或默认 admin/<随机一次性强密码>)。
import os
import sqlite3
import threading
import time
import uuid
import secrets
import logging

import config

DB_PATH = os.environ.get('USERS_DB', os.path.join(config.HERE, 'users.db'))

_lock = threading.Lock()
_log = logging.getLogger('user_store')


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock:
        c = _conn()
        try:
            c.execute('''CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                display_name TEXT,
                created_at REAL NOT NULL
            )''')
            c.commit()
        finally:
            c.close()
    _bootstrap_admin()


def _bootstrap_admin():
    """首次启动若无任何 admin, 自动建第一个 admin (env 指定或默认随机密码)。"""
    try:
        c = _conn()
        try:
            row = c.execute('SELECT COUNT(*) AS n FROM users WHERE is_admin=1').fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return
    if row and row['n'] > 0:
        return
    username = os.environ.get('ADMIN_BOOTSTRAP_USER', 'admin').strip() or 'admin'
    password = os.environ.get('ADMIN_BOOTSTRAP_PASSWORD', '').strip()
    generated = False
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True
    create_user(username, password, is_admin=True, display_name='Administrator')
    if generated:
        # 随机密码只打印一次到日志, 不进 .env, 不回显给用户 (与密钥安全要求一致)
        _log.warning('[bootstrap] 已创建初始 admin 用户 "%s", 一次性随机密码: %s', username, password)
        print(f'[bootstrap] 初始 admin 用户 "{username}" 已创建, 一次性随机密码见 server 日志', flush=True)
    else:
        _log.info('[bootstrap] 已按 env 创建初始 admin 用户 "%s"', username)
        print(f'[bootstrap] 初始 admin 用户 "{username}" 已创建 (env 指定密码)', flush=True)


def _hash(password):
    from werkzeug.security import generate_password_hash
    return generate_password_hash(password)


def verify_password(password_hash, password):
    from werkzeug.security import check_password_hash
    try:
        return check_password_hash(password_hash, password)
    except (ValueError, TypeError):
        return False


def create_user(username, password, is_admin=False, display_name=None):
    """新建用户。username 冲突抛 ValueError。返回新 user dict。"""
    username = (username or '').strip()
    if not username or not password:
        raise ValueError('username 和 password 不能为空')
    uid = uuid.uuid4().hex[:12]
    now = time.time()
    with _lock:
        c = _conn()
        try:
            try:
                c.execute('INSERT INTO users (id, username, password_hash, is_admin, display_name, created_at) '
                          'VALUES (?, ?, ?, ?, ?, ?)',
                          (uid, username, _hash(password), 1 if is_admin else 0, display_name, now))
                c.commit()
            except sqlite3.IntegrityError:
                raise ValueError(f'用户名已存在: {username}')
        finally:
            c.close()
    return {'id': uid, 'username': username, 'is_admin': bool(is_admin),
            'display_name': display_name, 'created_at': now}


def get_by_username(username):
    c = _conn()
    try:
        row = c.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        return _row_to_dict(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        c.close()


def get(user_id):
    c = _conn()
    try:
        row = c.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        return _row_to_dict(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        c.close()


def list_all():
    c = _conn()
    try:
        out = []
        for row in c.execute('SELECT * FROM users ORDER BY created_at ASC'):
            out.append(_row_to_dict(row))
        return out
    except sqlite3.Error:
        return []
    finally:
        c.close()


def update_password(user_id, new_password):
    if not new_password:
        raise ValueError('password 不能为空')
    with _lock:
        c = _conn()
        try:
            cur = c.execute('UPDATE users SET password_hash=? WHERE id=?',
                            (_hash(new_password), user_id))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()


def set_admin(user_id, is_admin):
    with _lock:
        c = _conn()
        try:
            cur = c.execute('UPDATE users SET is_admin=? WHERE id=?',
                            (1 if is_admin else 0, user_id))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()


def delete_user(user_id):
    with _lock:
        c = _conn()
        try:
            cur = c.execute('DELETE FROM users WHERE id=?', (user_id,))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()


def _row_to_dict(row):
    return {
        'id': row['id'],
        'username': row['username'],
        'password_hash': row['password_hash'],
        'is_admin': bool(row['is_admin']),
        'display_name': row['display_name'],
        'created_at': row['created_at'],
    }


def public_dict(u):
    """对外返回的安全形态 (不含 password_hash)。"""
    if not u:
        return None
    return {
        'id': u['id'],
        'username': u['username'],
        'is_admin': u['is_admin'],
        'display_name': u['display_name'],
        'created_at': u['created_at'],
    }


# 导入时初始化表 + bootstrap admin
try:
    init()
except Exception as e:
    print(f'[user_store] init 失败: {e}', flush=True)
