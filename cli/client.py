# cli/client.py — render_server 的统一 HTTP 客户端 (SDK 层)
# 供 CLI / MCP / template_engine / agent_demo 复用, 消除各处手写 requests 的重复.
import os
import time

import requests

import config


class ApiError(Exception):
    """服务端返回错误或网络异常"""

    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class ApiClient:
    """render_server 的薄客户端.

    base_url 解析顺序: 显式参数 > AUTOCUT_API 环境变量 > config.API_BASE.
    """

    def __init__(self, base_url=None, post_timeout=600, get_timeout=30):
        self.base_url = (base_url or os.environ.get('AUTOCUT_API')
                         or config.API_BASE).rstrip('/')
        self.post_timeout = post_timeout
        self.get_timeout = get_timeout
        self.session = requests.Session()

    # ---------- 底层 ----------

    def _url(self, endpoint):
        return f"{self.base_url}/{endpoint.lstrip('/')}"

    def post(self, endpoint, json=None, files=None, data=None, timeout=None):
        r = self.session.post(self._url(endpoint), json=json, files=files,
                              data=data, timeout=timeout or self.post_timeout)
        return self._parse(r)

    def get(self, endpoint, timeout=None):
        r = self.session.get(self._url(endpoint), timeout=timeout or self.get_timeout)
        return self._parse(r)

    def _parse(self, r):
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            msg = data.get('error') if isinstance(data, dict) else None
            raise ApiError(msg or f'HTTP {r.status_code}', status=r.status_code, payload=data)
        return data

    # ---------- 素材感知 ----------

    def perceive(self, path, do_asr=True, frames=4, force=False):
        return self.post('api/perceive', json={
            'path': path, 'do_asr': do_asr, 'frames': frames, 'force': force,
        })

    def perceive_upload(self, video_path, do_asr=True, frames=5):
        """multipart 上传视频文件分析 (perceive/video)."""
        with open(video_path, 'rb') as f:
            return self.post('perceive/video', files={'video': f},
                             data={'do_asr': str(do_asr).lower(), 'frames': str(frames)})

    # ---------- 草稿编辑 ----------

    def create_draft(self, width=1080, height=1920):
        return self.post('create_draft', json={'width': width, 'height': height})

    def add_video(self, draft_id, video_url, **kw):
        return self.post('add_video', json={'draft_id': draft_id, 'video_url': video_url, **kw})

    def add_text(self, draft_id, text, **kw):
        return self.post('add_text', json={'draft_id': draft_id, 'text': text, **kw})

    def add_audio(self, draft_id, audio_url, **kw):
        return self.post('add_audio', json={'draft_id': draft_id, 'audio_url': audio_url, **kw})

    def add_image(self, draft_id, image_url, **kw):
        return self.post('add_image', json={'draft_id': draft_id, 'image_url': image_url, **kw})

    def save_draft(self, draft_id, draft_folder=None):
        data = {'draft_id': draft_id}
        if draft_folder:
            data['draft_folder'] = draft_folder
        return self.post('save_draft', json=data)

    def list_drafts(self):
        return self.get('api/drafts')

    def delete_draft(self, folder):
        r = self.session.delete(self._url(f'api/drafts/{folder}'), timeout=self.get_timeout)
        return self._parse(r)

    # ---------- 渲染 ----------

    def render(self, draft_id):
        """提交渲染 (draft_id 或草稿文件夹名), 返回 {'task_id', ...}."""
        return self.post(f'render/draft/{draft_id}')

    def render_zip(self, zip_path, draft_name=None):
        """上传 zip 草稿渲染 (multipart)."""
        with open(zip_path, 'rb') as f:
            files = {'draft': f}
            data = {'draft_name': draft_name} if draft_name else None
            return self.post('render', files=files, data=data)

    def render_status(self, task_id):
        return self.get(f'render/status/{task_id}')

    def render_list(self):
        return self.get('render/list')

    def render_wait(self, task_id, timeout=600, poll=2.0, on_progress=None):
        """轮询渲染任务直到 done/error. 返回最终 status dict.
        on_progress(status_dict) 每轮调用一次 (status 含 progress 字段)."""
        start = time.time()
        while True:
            st = self.render_status(task_id)
            if isinstance(st, dict) and 'error' in st and 'status' not in st:
                raise ApiError(st.get('error', 'unknown task'), status=404, payload=st)
            if on_progress:
                on_progress(st)
            if st.get('status') in ('done', 'error'):
                return st
            if time.time() - start > timeout:
                raise ApiError(f'render timeout ({timeout}s)', payload=st)
            time.sleep(poll)

    def render_download(self, task_id, dest):
        """下载渲染结果 mp4 到 dest. 返回保存路径."""
        r = self.session.get(self._url(f'render/download/{task_id}'),
                             timeout=self.post_timeout, stream=True)
        if r.status_code != 200:
            self._raise_http(r)
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        return dest

    def _raise_http(self, r):
        try:
            payload = r.json()
            msg = payload.get('error') if isinstance(payload, dict) else str(payload)
        except ValueError:
            msg = r.text[:200] or f'HTTP {r.status_code}'
        raise ApiError(msg or f'HTTP {r.status_code}', status=r.status_code)

    # ---------- 模板 ----------

    def list_templates(self):
        return self.get('api/templates')

    def render_template(self, template, variables=None, do_render=False):
        return self.post('api/templates/render', json={
            'template': template, 'variables': variables or {}, 'render': do_render,
        })

    # ---------- 系统 ----------

    def health(self):
        return self.get('health')

    def localsend_start(self):
        return self.post('api/localsend/start')

    def localsend_stop(self):
        return self.post('api/localsend/stop')

    def localsend_status(self):
        return self.get('api/localsend/status')


# 模块级单例 (供内部模块复用; CLI 可自行实例化以指定 base_url)
_client = None


def get_client(base_url=None):
    global _client
    if _client is None or base_url:
        _client = ApiClient(base_url=base_url)
    return _client
