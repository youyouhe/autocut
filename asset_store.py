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
    c.execute('CREATE INDEX IF NOT EXISTS idx_assets_name ON assets(name)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_assets_type ON assets(type)')
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


def upsert(path, result):
    with _lock:
        _upsert(path, result)


def _upsert(path, result):
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
        c.execute('''INSERT OR REPLACE INTO assets
            (path, name, type, duration, width, height, visual, audio_text,
             tags, analysis_mode, payload, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (path, name, ftype,
             meta.get('duration'), meta.get('width'), meta.get('height'),
             visual, audio_text, json.dumps(tags, ensure_ascii=False),
             data.get('analysis_mode'), json.dumps(data, ensure_ascii=False), now))
        c.execute('DELETE FROM asset_tags WHERE asset_path=?', (path,))
        c.executemany('INSERT OR IGNORE INTO asset_tags (asset_path, tag) VALUES (?,?)',
                      [(path, t) for t in tags])
        c.commit()
    finally:
        c.close()


def get(path):
    """按完整路径取完整 result dict (含 _path/_time)。没找到返回 None。"""
    c = _conn()
    try:
        row = c.execute('SELECT payload FROM assets WHERE path=?', (path,)).fetchone()
        if not row:
            return None
        return json.loads(row['payload'])
    except (sqlite3.Error, ValueError):
        return None
    finally:
        c.close()


def get_by_name(name):
    """按 basename 精确匹配, 再退化到包含匹配(取最新一条)。"""
    c = _conn()
    try:
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


def has(path):
    c = _conn()
    try:
        return c.execute('SELECT 1 FROM assets WHERE path=?', (path,)).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        c.close()


def delete(path):
    with _lock:
        c = _conn()
        try:
            c.execute('DELETE FROM assets WHERE path=?', (path,))
            c.execute('DELETE FROM asset_tags WHERE asset_path=?', (path,))
            c.commit()
        finally:
            c.close()


def search_tags(keywords, type=None, limit=100):
    """按标签检索 (SQL 索引查询, 任一关键词是任一标签的子串)。"""
    keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not keywords:
        return []
    c = _conn()
    try:
        like = ' OR '.join(['t.tag LIKE ?'] * len(keywords))
        params = [f'%{k}%' for k in keywords]
        sql = ('SELECT DISTINCT a.path, a.name, a.type, a.tags, a.duration, a.updated_at '
               'FROM assets a JOIN asset_tags t ON a.path = t.asset_path '
               f'WHERE ({like})')
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


def search_text(query, type=None, limit=50):
    """全文检索: 按空格分词, 每个词都须命中 name/画面描述/转录/标签 之一 (AND)。"""
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


def list_all(limit=None):
    """所有记录的摘要 (供统计/调试)。"""
    c = _conn()
    try:
        sql = ('SELECT path, name, type, tags, duration, visual, audio_text, analysis_mode, updated_at '
               'FROM assets ORDER BY updated_at DESC')
        params = []
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


def count():
    c = _conn()
    try:
        return c.execute('SELECT COUNT(*) AS n FROM assets').fetchone()['n']
    except sqlite3.Error:
        return 0
    finally:
        c.close()


try:
    init()
except Exception:
    pass
