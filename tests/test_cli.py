# tests/test_cli.py — CLI 与 ApiClient 单元测试 (mock requests, 不依赖真实服务)
import json

import pytest


# ============================================================ ApiClient

def _make_client(monkeypatch, responses):
    """构造 ApiClient, mock 掉 Session.request 返回预设 responses."""
    from cli.client import ApiClient

    class FakeResp:
        def __init__(self, data, status=200):
            self._data = data
            self.status_code = status

        def json(self):
            return self._data

    class FakeSession:
        def __init__(self):
            self.calls = []
            self.responses = list(responses)

        def post(self, url, **kw):
            self.calls.append(('POST', url, kw))
            return FakeResp(self.responses.pop(0))

        def get(self, url, **kw):
            self.calls.append(('GET', url, kw))
            return FakeResp(self.responses.pop(0))

        def delete(self, url, **kw):
            self.calls.append(('DELETE', url, kw))
            return FakeResp(self.responses.pop(0))

    client = ApiClient(base_url='http://x:9010')
    client.session = FakeSession()
    return client, client.session


def test_client_base_url():
    from cli.client import ApiClient
    c = ApiClient(base_url='http://h:1')
    assert c.base_url == 'http://h:1'
    assert c._url('render/draft/abc') == 'http://h:1/render/draft/abc'
    assert c._url('/health') == 'http://h:1/health'


def test_client_create_draft(monkeypatch):
    c, s = _make_client(monkeypatch, [
        {'success': True, 'output': {'draft_id': 'dfd_1'}}])
    r = c.create_draft(width=1080, height=1920)
    assert r['output']['draft_id'] == 'dfd_1'
    assert s.calls[0][0] == 'POST'
    assert s.calls[0][1].endswith('/create_draft')
    assert s.calls[0][2]['json'] == {'width': 1080, 'height': 1920}


def test_client_render_poll(monkeypatch):
    c, s = _make_client(monkeypatch, [
        {'status': 'rendering', 'progress': {'stage': 'export', 'pct': 50}},
        {'status': 'done', 'mp4_name': 'a.mp4'},
    ])
    seen = []
    final = c.render_wait('t1', timeout=30, poll=0, on_progress=seen.append)
    assert final['status'] == 'done'
    assert len(seen) == 2
    assert seen[0]['progress']['stage'] == 'export'


def test_client_render_wait_timeout(monkeypatch):
    c, s = _make_client(monkeypatch, [{'status': 'rendering'}] * 200)
    with pytest.raises(Exception, match='timeout'):
        c.render_wait('t1', timeout=0.01, poll=0)


def test_client_raises_on_http_error(monkeypatch):
    """HTTP 4xx/5xx 应抛 ApiError (退出码非零的关键)."""
    from cli.client import ApiClient, ApiError

    class FakeResp:
        status_code = 404

        def json(self):
            return {'error': 'unknown task'}

    class FakeSession:
        def get(self, url, **kw):
            return FakeResp()

    c = ApiClient(base_url='http://x:1')
    c.session = FakeSession()
    with pytest.raises(ApiError, match='unknown task'):
        c.get('render/status/abc')


def test_client_ok_with_success_false(monkeypatch):
    """业务失败 (200 + success:false) 不抛异常, 留给命令层判断."""
    c, s = _make_client(monkeypatch, [{'success': False, 'error': 'boom'}])
    r = c.create_draft()
    assert r['success'] is False


# ============================================================ CLI 命令 (mock client)

def _run_cli(monkeypatch, argv, responses, capsys):
    from cli.client import ApiClient

    class FakeResp:
        def __init__(self, data):
            self._data = data
            self.status_code = 200

        def json(self):
            return self._data

    class FakeSession:
        def __init__(self):
            self.responses = list(responses)

        def post(self, url, **kw):
            return FakeResp(self.responses.pop(0))

        def get(self, url, **kw):
            return FakeResp(self.responses.pop(0))

        def delete(self, url, **kw):
            return FakeResp(self.responses.pop(0))

    real_client = ApiClient(base_url='http://x:1')
    real_client.session = FakeSession()
    monkeypatch.setattr('cli.client._client', real_client)
    monkeypatch.setattr('autocut_cli._client',
                        lambda args: real_client)

    import autocut_cli
    try:
        autocut_cli.main(argv)
    except SystemExit as e:
        assert e.code == 0


def test_cli_health_json(monkeypatch, capsys):
    _run_cli(monkeypatch, ['health', '--json'], [{'ok': True}], capsys)
    out = capsys.readouterr().out
    assert json.loads(out) == {'ok': True}


def test_cli_draft_create_prints_id(monkeypatch, capsys):
    _run_cli(monkeypatch, ['draft', 'create'], [
        {'success': True, 'output': {'draft_id': 'dfd_x'}}], capsys)
    out = capsys.readouterr().out
    assert 'dfd_x' in out


def test_cli_render_no_wait_prints_poll(monkeypatch, capsys):
    _run_cli(monkeypatch, ['render', 'dfd_x'], [
        {'success': True},              # save_draft
        {'task_id': 't9', 'status': 'queued'},  # render
    ], capsys)
    out = capsys.readouterr().out
    assert 't9' in out
    assert 'poll' in out


def test_cli_render_status_json(monkeypatch, capsys):
    _run_cli(monkeypatch, ['render-status', 't9', '--json'], [
        {'status': 'done', 'mp4_name': 'a.mp4'}], capsys)
    out = capsys.readouterr().out
    assert json.loads(out)['status'] == 'done'


def test_cli_parser_rejects_unknown():
    import autocut_cli
    with pytest.raises(SystemExit):
        autocut_cli.build_parser().parse_args(['nonsense'])
