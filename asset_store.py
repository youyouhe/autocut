# asset_store.py — 素材分析结果 SQLite 持久化 (stdlib sqlite3)
# 素材的 VLM/ASR 解析结果(画面描述/标签/转录/字幕等)落库, 长期管理 + 快速检索。
# 替代原来 analysis_cache/*.json + 内存 dict 的方案; 检索走 SQL, 不逐文件读 JSON。
import os
import json
import sqlite3
import threading
import time

import config

DB_PATH = os.environ.get('ASSETS_DB', os.path.join(config.HERE, 'assets.db'))

VIDEO_EXTS = ('.mp4', '.mov', '.avi', '.mkv')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp')
AUDIO_EXTS = ('.mp3', '.wav', '.aac', '.m4a')

_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _classify(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        return 'video'
    if ext in IMAGE_EXTS:
        return 'image'
    if ext in AUDIO_EXTS:
        return 'audio'
    return 'other'


def _init_schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS assets (
        path TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        duration REAL,
        width INTEGER,
        height INTEGER,
        visual TEXT,
        audio_text TEXT,
        tags TEXT,
        analysis_mode TEXT,
        payload TEXT NOT NULL,
        updated_at REAL NOT NULL
    )''')
    # 多租户迁移: 补 owner 列 (单租户历史行 owner 留 NULL, 视为 legacy 全局)
    cols = {r['name'] for r in c.execute('PRAGMA table_info(assets)').fetchall()}
    if 'owner' not in cols:
        c.execute('ALTER TABLE assets ADD COLUMN owner TEXT')
    c.execute('CREATE INDEX IF NOT EXISTS idx_assets_name ON assets(name)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_assets_type ON assets(type)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_assets_owner ON assets(owner)')
    c.execute('''CREATE TABLE IF NOT EXISTS asset_tags (
        asset_path TEXT NOT NULL,
        tag TEXT NOT NULL,
        PRIMARY KEY (asset_path, tag)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_asset_tags_tag ON asset_tags(tag)')


def init():
    with _lock:
        c = _conn()
        try:
            _init_schema(c)
            c.commit()
        finally:
            c.close()
    _migrate_from_json()


def _migrate_from_json():
    """首次升级: 把旧的 analysis_cache/*.json 迁进 SQLite (库里已有数据就跳过)。"""
    if not os.path.isdir(config.CACHE_DIR):
        return
    try:
        jsons = [f for f in os.listdir(config.CACHE_DIR) if f.endswith('.json')]
    except OSError:
        return
    if not jsons:
        return
    c = _conn()
    try:
        n = c.execute('SELECT COUNT(*) AS n FROM assets').fetchone()['n']
    except sqlite3.Error:
        n = 0
    finally:
        c.close()
    if n:
        return
    imported = 0
    for f in jsons:
        try:
            with open(os.path.join(config.CACHE_DIR, f), encoding='utf-8') as fh:
                data = json.load(fh)
            path = data.get('_path')
            if not path:
                continue
            _upsert(path, data)
            imported += 1
        except Exception:
            continue
    if imported:
        print(f'[asset_store] 从 analysis_cache 迁移 {imported} 条分析记录到 SQLite', flush=True)


def upsert(path, result, owner=None):
    with _lock:
        _upsert(path, result, owner)


def _upsert(path, result, owner=None):
    data = dict(result)
    now = data.get('_time') or time.time()
    data['_path'] = path
    data['_time'] = now

    name = os.path.basename(path)
    ftype = _classify(path)
    meta = data.get('meta') or {}
    tags = data.get('tags') or []
    if not isinstance(tags, list):
        tags = [str(t) for t in tags if t]
    tags = [str(t) for t in tags if str(t).strip()]
    audio = data.get('audio') or {}
    audio_text = audio.get('full_text', '') if isinstance(audio, dict) else ''
    visual = data.get('visual_analysis') or ''

    c = _conn()
    try:
        # 保留已存在的 owner (若本次 upsert 未传 owner, 不覆盖归属)
        if owner is None:
            row = c.execute('SELECT owner FROM assets WHERE path=?', (path,)).fetchone()
            owner = row['owner'] if row else None
        c.execute('''INSERT OR REPLACE INTO assets
            (path, name, type, duration, width, height, visual, audio_text,
             tags, analysis_mode, payload, updated_at, owner)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (path, name, ftype,
             meta.get('duration'), meta.get('width'), meta.get('height'),
             visual, audio_text, json.dumps(tags, ensure_ascii=False),
             data.get('analysis_mode'), json.dumps(data, ensure_ascii=False), now, owner))
        c.execute('DELETE FROM asset_tags WHERE asset_path=?', (path,))
        c.executemany('INSERT OR IGNORE INTO asset_tags (asset_path, tag) VALUES (?,?)',
                      [(path, t) for t in tags])
        c.commit()
    finally:
        c.close()


def get(path, owner=None):
    """按完整路径取完整 result dict (含 _path/_time)。没找到返回 None。
    owner 给定时: 仅返回 owner 匹配 (或 legacy NULL) 的记录。"""
    c = _conn()
    try:
        if owner is not None:
            row = c.execute(
                'SELECT payload FROM assets WHERE path=? AND (owner=? OR owner IS NULL)',
                (path, owner)
            ).fetchone()
        else:
            row = c.execute('SELECT payload FROM assets WHERE path=?', (path,)).fetchone()
        if not row:
            return None
        return json.loads(row['payload'])
    except (sqlite3.Error, ValueError):
        return None
    finally:
        c.close()


def get_by_name(name, owner=None):
    """按 basename 精确匹配, 再退化到包含匹配(取最新一条)。
    owner 给定时: 仅在 owner 匹配 (或 legacy NULL) 的记录里匹配。"""
    c = _conn()
    try:
        if owner is not None:
            filt = 'AND (owner=? OR owner IS NULL)'
            row = c.execute(
                f'SELECT payload FROM assets WHERE name=? {filt} ORDER BY updated_at DESC LIMIT 1',
                (name, owner)
            ).fetchone()
            if row:
                return json.loads(row['payload'])
            row = c.execute(
                f'SELECT payload FROM assets WHERE name LIKE ? {filt} ORDER BY updated_at DESC LIMIT 1',
                (f'%{name}%', owner)
            ).fetchone()
        else:
            row = c.execute('SELECT payload FROM assets WHERE name=?', (name,)).fetchone()
            if row:
                return json.loads(row['payload'])
            row = c.execute('SELECT payload FROM assets WHERE name LIKE ? ORDER BY updated_at DESC LIMIT 1',
                            (f'%{name}%',)).fetchone()
        if row:
            return json.loads(row['payload'])
        return None
    except (sqlite3.Error, ValueError):
        return None
    finally:
        c.close()


def has(path, owner=None):
    c = _conn()
    try:
        if owner is not None:
            return c.execute(
                'SELECT 1 FROM assets WHERE path=? AND (owner=? OR owner IS NULL)', (path, owner)
            ).fetchone() is not None
        return c.execute('SELECT 1 FROM assets WHERE path=?', (path,)).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        c.close()


def delete(path, owner=None):
    with _lock:
        c = _conn()
        try:
            if owner is not None:
                c.execute('DELETE FROM assets WHERE path=? AND (owner=? OR owner IS NULL)',
                          (path, owner))
                c.execute('DELETE FROM asset_tags WHERE asset_path=? AND asset_path NOT IN '
                          '(SELECT path FROM assets)', (path,))
            else:
                c.execute('DELETE FROM assets WHERE path=?', (path,))
                c.execute('DELETE FROM asset_tags WHERE asset_path=?', (path,))
            c.commit()
        finally:
            c.close()


def search_tags(keywords, type=None, limit=100, owner=None):
    """按标签检索 (SQL 索引查询, 任一关键词是任一标签的子串)。
    owner 给定时: 仅检索 owner 匹配 (或 legacy NULL) 的记录。"""
    keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not keywords:
        return []
    c = _conn()
    try:
        like = ' OR '.join(['t.tag LIKE ?'] * len(keywords))
        params = [f'%{k}%' for k in keywords]
        sql = ('SELECT DISTINCT a.path, a.name, a.type, a.tags, a.duration, a.updated_at, a.owner '
               'FROM assets a JOIN asset_tags t ON a.path = t.asset_path '
               f'WHERE ({like})')
        if owner is not None:
            sql += ' AND (a.owner=? OR a.owner IS NULL)'
            params.append(owner)
        if type:
            sql += ' AND a.type=?'
            params.append(type)
        sql += ' ORDER BY a.updated_at DESC LIMIT ?'
        params.append(limit)
        out = []
        for row in c.execute(sql, params):
            tags = json.loads(row['tags']) if row['tags'] else []
            out.append({'path': row['path'], 'name': row['name'], 'type': row['type'],
                        'tags': tags, 'duration': row['duration']})
        return out
    except sqlite3.Error:
        return []
    finally:
        c.close()


def search_text(query, type=None, limit=50, owner=None):
    """全文检索: 按空格分词, 每个词都须命中 name/画面描述/转录/标签 之一 (AND)。
    owner 给定时: 仅检索 owner 匹配 (或 legacy NULL) 的记录。"""
    terms = [t for t in (query or '').split() if t.strip()]
    if not terms:
        return []
    c = _conn()
    try:
        conds = []
        params = []
        for t in terms:
            conds.append('(a.name LIKE ? OR a.visual LIKE ? OR a.audio_text LIKE ? OR a.tags LIKE ?)')
            params += [f'%{t}%'] * 4
        sql = ('SELECT a.path, a.name, a.type, a.tags, a.duration, a.visual, a.audio_text, a.updated_at '
               'FROM assets a WHERE ' + ' AND '.join(conds))
        if owner is not None:
            sql += ' AND (a.owner=? OR a.owner IS NULL)'
            params.append(owner)
        if type:
            sql += ' AND a.type=?'
            params.append(type)
        sql += ' ORDER BY a.updated_at DESC LIMIT ?'
        params.append(limit)
        out = []
        for row in c.execute(sql, params):
            tags = json.loads(row['tags']) if row['tags'] else []
            out.append({
                'path': row['path'], 'name': row['name'], 'type': row['type'],
                'tags': tags, 'duration': row['duration'],
                'visual': (row['visual'] or '')[:200],
                'audio_text': (row['audio_text'] or '')[:200],
            })
        return out
    except sqlite3.Error:
        return []
    finally:
        c.close()


def list_all(limit=None, owner=None):
    """所有记录的摘要 (供统计/调试)。
    owner 给定时: 仅返回 owner 匹配 (或 legacy NULL) 的记录。"""
    c = _conn()
    try:
        sql = ('SELECT path, name, type, tags, duration, visual, audio_text, analysis_mode, updated_at, owner '
               'FROM assets')
        params = []
        if owner is not None:
            sql += ' WHERE (owner=? OR owner IS NULL)'
            params.append(owner)
        sql += ' ORDER BY updated_at DESC'
        if limit:
            sql += ' LIMIT ?'
            params.append(limit)
        out = []
        for row in c.execute(sql, params):
            tags = json.loads(row['tags']) if row['tags'] else []
            out.append({
                'path': row['path'], 'name': row['name'], 'type': row['type'],
                'tags': tags, 'duration': row['duration'],
                'analysis_mode': row['analysis_mode'],
                'has_visual': bool(row['visual']), 'has_audio': bool(row['audio_text']),
                'time': row['updated_at'],
            })
        return out
    except sqlite3.Error:
        return []
    finally:
        c.close()


def count(owner=None):
    c = _conn()
    try:
        if owner is not None:
            return c.execute(
                'SELECT COUNT(*) AS n FROM assets WHERE (owner=? OR owner IS NULL)', (owner,)
            ).fetchone()['n']
        return c.execute('SELECT COUNT(*) AS n FROM assets').fetchone()['n']
    except sqlite3.Error:
        return 0
    finally:
        c.close()


try:
    init()
except Exception:
    pass
