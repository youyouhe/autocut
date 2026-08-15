# tests/test_core.py — 纯逻辑单元测试 (不依赖 Windows/剪映)
import io
import os
import zipfile

import pytest

import config


# ============================================================ config 安全工具

def test_is_within():
    assert config.is_within('/a/b', '/a/b/c.txt')
    assert config.is_within('/a/b', '/a/b')
    assert config.is_within('/a/b', '/a/c/../b/x')  # .. 解析后仍在内部
    assert not config.is_within('/a/b', '/a/bc.txt')
    assert not config.is_within('/a/b', '/etc/passwd')


def test_safe_folder_name():
    assert config.safe_folder_name('8月11日')
    assert config.safe_folder_name('my_draft')
    assert not config.safe_folder_name('..')
    assert not config.safe_folder_name('.')
    assert not config.safe_folder_name('')
    assert not config.safe_folder_name('a/b')
    assert not config.safe_folder_name('a\\b')


def test_safe_zip_extract_rejects_traversal(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('../evil.txt', 'pwned')
    buf.seek(0)
    dest = tmp_path / 'out'
    dest.mkdir()
    with zipfile.ZipFile(buf) as zf:
        with pytest.raises(ValueError):
            config.safe_zip_extract(zf, str(dest))
    assert not (tmp_path / 'evil.txt').exists()


def test_safe_zip_extract_ok(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('draft/draft_content.json', '{}')
    buf.seek(0)
    dest = tmp_path / 'out'
    dest.mkdir()
    with zipfile.ZipFile(buf) as zf:
        config.safe_zip_extract(zf, str(dest))
    assert (dest / 'draft' / 'draft_content.json').exists()


# ============================================================ template_engine

def test_parse_time():
    from template_engine import _parse_time
    assert _parse_time('3s') == 3.0
    assert _parse_time('1.5s') == 1.5
    assert _parse_time(10) == 10
    assert _parse_time(None, default=2) == 2
    assert _parse_time('abc', default=7) == 7


def test_fill_variables():
    from template_engine import _fill
    tpl = {'title': '{{name}} 的旅程', 'items': ['{{name}}', 3]}
    out = _fill(tpl, {'name': '旅行'})
    assert out['title'] == '旅行 的旅程'
    assert out['items'][0] == '旅行'
    assert out['items'][1] == 3


# ============================================================ perceive ASR 解析

def test_parse_srt():
    from perceive import _parse_srt
    srt = ("1\n00:00:01,000 --> 00:00:03,500\n你好 世界\n\n"
           "2\n00:00:04,000 --> 00:00:06,000\n第二句\n")
    segs = _parse_srt(srt)
    assert len(segs) == 2
    assert segs[0]['start'] == 1.0
    assert segs[0]['end'] == 3.5
    assert segs[0]['text'] == '你好 世界'


def test_parse_asr_response_json_list():
    from perceive import _parse_asr_response
    raw = '[{"start": 0, "end": 2, "text": "hi"}, {"start_time": 2, "end_time": 4, "text": "yo"}]'
    r = _parse_asr_response(raw)
    assert len(r['segments']) == 2
    assert r['full_text'] == 'hi yo'


def test_parse_asr_response_wrapped_srt():
    from perceive import _parse_asr_response
    srt = '1\n00:00:00,000 --> 00:00:02,000\n你好\n'
    import json
    r = _parse_asr_response(json.dumps({'code': 0, 'data': srt}))
    assert r['full_text'] == '你好'


def test_parse_asr_response_plain_text():
    from perceive import _parse_asr_response
    r = _parse_asr_response('纯文本转录结果')
    assert r['full_text'] == '纯文本转录结果'


# ============================================================ localsend_recv

def test_localsend_safe_name():
    from localsend_recv import LSHandler
    assert LSHandler._safe_name('../../etc/passwd') == 'passwd'
    assert LSHandler._safe_name('a/b/c.mp4') == 'c.mp4'
    assert LSHandler._safe_name('') == 'recv.bin'


def test_localsend_dedup_path(tmp_path):
    from localsend_recv import LSHandler
    p = tmp_path / 'v.mp4'
    assert LSHandler._dedup_path(str(p)) == str(p)
    p.write_bytes(b'x')
    assert LSHandler._dedup_path(str(p)) == str(tmp_path / 'v (1).mp4')


# ============================================================ memory_store

def test_memory_store_analysis_roundtrip(tmp_path, monkeypatch):
    import memory_store as ms
    monkeypatch.setattr(ms, 'CACHE_DIR', str(tmp_path))
    ms.save_analysis('/tmp/x.mp4', {'meta': {'duration': 5}})
    assert ms.has_analysis('/tmp/x.mp4')
    got = ms.get_analysis('/tmp/x.mp4')
    assert got['meta']['duration'] == 5
    assert got['_path'] == '/tmp/x.mp4'
    # 落盘文件存在
    assert any(f.endswith('.json') for f in os.listdir(tmp_path))


def test_memory_store_video_cache(tmp_path, monkeypatch):
    import memory_store as ms
    monkeypatch.setattr(ms, 'MAX_TOTAL_RAM', 100)
    monkeypatch.setattr(ms, 'MAX_VIDEO_RAM', 100)
    f1 = tmp_path / 'a.mp4'
    f1.write_bytes(b'1' * 60)
    f2 = tmp_path / 'b.mp4'
    f2.write_bytes(b'2' * 60)
    with ms._lock:
        ms._video_store.clear()
        ms._video_ram_used = 0
    assert ms.maybe_load_video(str(f1))
    assert ms.maybe_load_video(str(f2))  # 触发淘汰 f1
    assert ms.get_video_bytes(str(f2)) == b'2' * 60
    stats = ms.video_cache_stats()
    assert stats['videos_in_ram'] == 1
    assert stats['ram_used_mb'] == round(60 / 1024 / 1024, 1)


# ============================================================ task_store

def test_task_store_roundtrip(tmp_path, monkeypatch):
    import task_store as ts
    monkeypatch.setattr(ts, 'DB_PATH', str(tmp_path / 'tasks.db'))
    ts.init()
    ts.upsert({'task_id': 't1', 'status': 'done', 'mp4_name': 'a.mp4',
               'draft_name': 'd', 'progress': {'stage': 'done', 'pct': 100}})
    ts.upsert({'task_id': 't2', 'status': 'queued', 'draft_name': 'd2'})
    loaded = ts.load_all()
    assert loaded['t1']['status'] == 'done'
    assert loaded['t1']['mp4_name'] == 'a.mp4'
    assert loaded['t2']['status'] == 'queued'
    ts.delete('t2')
    assert 't2' not in ts.load_all()


# ============================================================ ASR local fallback

def test_asr_local_without_faster_whisper(monkeypatch):
    import perceive
    monkeypatch.setattr(perceive.config, 'ASR_BACKEND', 'local')
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *a, **k):
        if name == 'faster_whisper':
            raise ImportError('no faster_whisper')
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', fake_import)
    r = perceive.asr_transcribe_local('/nonexistent.mp4')
    assert 'error' in r
    assert 'faster-whisper' in r['error']


# ============================================================ render progress 解析

def test_render_progress_parse_line():
    import render_server as rs
    assert rs._parse_progress_line('[PROGRESS] {"stage": "inject", "pct": 10}') == \
        {'stage': 'inject', 'pct': 10}
    assert rs._parse_progress_line('[PROGRESS] {"stage": "rendering", "pct": null, "temp_bytes": 123}') == \
        {'stage': 'rendering', 'pct': None, 'temp_bytes': 123}
    assert rs._parse_progress_line('[12:00:00] normal log line') is None
    assert rs._parse_progress_line('[PROGRESS] not json') is None


def test_render_progress_streaming(tmp_path):
    # 用假脚本输出 [PROGRESS] 行, 走真实读线程路径验证进度回传
    import render_server as rs
    import sys
    fake = tmp_path / 'fake_driver.py'
    fake.write_text(
        "print('[PROGRESS] {\"stage\": \"inject\", \"pct\": 10}')\n"
        "print('[PROGRESS] {\"stage\": \"rendering\", \"pct\": null, \"temp_bytes\": 456}')\n"
        "print('normal log')\n"
    )
    task_id = 'streamtest'
    with rs.TASK_LOCK:
        rs.tasks[task_id] = {}
    code, out, err = rs._stream_process([sys.executable, str(fake)], task_id)
    assert code == 0
    assert 'normal log' in out
    assert rs.tasks[task_id]['progress']['stage'] == 'rendering'
    assert rs.tasks[task_id]['progress']['temp_bytes'] == 456
    with rs.TASK_LOCK:
        rs.tasks.pop(task_id, None)
