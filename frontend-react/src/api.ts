// 真实后端 API 封装 — 全部指向 Flask render_server (:9002, 同源).
// 端点契约见 SYSTEM_MANUAL.md §16 与 render_server.py / capcut_server.py.

// ---------- 类型 (按真实返回字段) ----------
export type AssetType = 'video' | 'image' | 'audio' | 'other';

export interface Asset {
  name: string;
  path: string;
  file: string;
  type: AssetType;
  size?: number;
  modified_at?: string;
  analysis?: PerceiveResult | { error?: string } | null;
  _cached?: boolean;
  _duration?: string;
  _portrait?: boolean;
}

export interface Draft {
  id: string;
  name: string;
  duration: number;       // 秒
  created: number;        // 秒级 epoch
  modified: number;       // 秒级 epoch
  folder: string;
  cover_url: string | null;
  size_bytes?: number;
  type?: string;
}

export type RenderStatus = 'queued' | 'rendering' | 'done' | 'error';

export interface RenderProgress {
  stage?: string | null;
  pct?: number | null;
  elapsed?: number | null;
  temp_bytes?: number | null;
}

export interface RenderTask {
  task_id: string;
  status: RenderStatus;
  mp4?: string;
  mp4_name?: string;
  draft?: string;
  draft_name?: string;
  duration?: number;
  error?: string;
  created?: number;
  progress?: RenderProgress;
}

export interface PerceiveResult {
  meta?: { duration?: number; width?: number; height?: number; fps?: number; source_path?: string };
  scenes?: number[];   // 镜头切换时间点 (秒)
  visual_analysis?: string;   // 含 JSON 块
  audio?: { full_text?: string; segments?: any[]; asr_model?: string };
  srt?: string;   // 标准 SRT 字幕文本
  _cached?: boolean;
  created_at?: string;
  error?: string;
  [k: string]: any;
}

export interface LocalSendStatus {
  running: boolean;
  alias?: string;
  port?: number;
  my_ip?: string;
  active_sender?: string | null;
  pending?: { fileName: string; size: number; fileType: string; done: boolean }[];
  pending_files?: number;
  received?: { fileName: string; size: number; fileType: string; sender_ip: string; time: number; saved_path: string }[];
  received_count?: number;
  error?: string;
}

// ---------- 基础 ----------
async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { const e = await r.json(); msg = e.error || JSON.stringify(e); } catch { /* ignore */ }
    throw new Error(msg);
  }
  return r.json() as Promise<T>;
}

// ---------- 素材 ----------
export function getAssets(): Promise<Asset[]> {
  return jsonFetch<{ assets: Asset[] }>('/api/assets').then(d => d.assets || []);
}

export async function upload(files: FileList | File[]): Promise<Asset[]> {
  const fd = new FormData();
  for (const f of Array.from(files)) fd.append('files', f);
  const data = await jsonFetch<{ assets: Asset[] }>('/api/upload', { method: 'POST', body: fd });
  return data.assets || [];
}

/** 视频字节 URL (hover 预览 / <video src>) */
export function serveUrl(path: string): string {
  return `/api/video/serve?path=${encodeURIComponent(path)}`;
}

export function perceive(path: string, opts?: { force?: boolean; do_asr?: boolean; frames?: number }): Promise<PerceiveResult> {
  return jsonFetch<PerceiveResult>('/api/perceive', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, force: opts?.force, do_asr: opts?.do_asr ?? true, frames: opts?.frames ?? 4 }),
  });
}

export function checkCached(path: string): Promise<{ cached: boolean; result?: PerceiveResult }> {
  return jsonFetch(`/api/perceive/cached?path=${encodeURIComponent(path)}`);
}

// ---------- 草稿 ----------
export function listDrafts(): Promise<Draft[]> {
  return jsonFetch<Draft[]>('/api/drafts');
}

export function deleteDraft(folder: string): Promise<{ ok: boolean }> {
  return jsonFetch(`/api/drafts/${encodeURIComponent(folder)}`, { method: 'DELETE' });
}

export interface CreateDraftResult { success: boolean; output?: { draft_id: string; draft_url: string }; error?: string }
export function createDraft(width = 1080, height = 1920): Promise<CreateDraftResult> {
  return jsonFetch<CreateDraftResult>('/create_draft', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ width, height }),
  });
}

export function addVideo(draft_id: string, video_url: string, opts?: { start?: number; end?: number; target_start?: number; volume?: number; transition?: string }) {
  return jsonFetch('/add_video', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ draft_id, video_url, ...opts }),
  });
}

export function addText(draft_id: string, text: string, opts?: { start?: number; end?: number; font_size?: number; font_color?: string; transform_x?: number; transform_y?: number }) {
  return jsonFetch('/add_text', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ draft_id, text, start: opts?.start ?? 0, end: opts?.end ?? 5, font_size: opts?.font_size ?? 12, font_color: opts?.font_color ?? '#FFFFFF', transform_x: opts?.transform_x ?? 0, transform_y: opts?.transform_y ?? 0 }),
  });
}

export function saveDraft(draft_id: string): Promise<{ success: boolean; error?: string }> {
  return jsonFetch('/save_draft', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ draft_id }),
  });
}

// ---------- 渲染 ----------
export function renderDraft(draft_id: string): Promise<{ task_id: string; status: string; poll: string; error?: string }> {
  return jsonFetch(`/render/draft/${encodeURIComponent(draft_id)}`, { method: 'POST' });
}

export function renderStatus(task_id: string): Promise<RenderTask> {
  return jsonFetch<RenderTask>(`/render/status/${encodeURIComponent(task_id)}`);
}

export function renderList(): Promise<RenderTask[]> {
  return jsonFetch<RenderTask[]>('/render/list');
}

export function downloadUrl(task_id: string): string {
  return `/render/download/${encodeURIComponent(task_id)}`;
}

// ---------- 对话 (SSE) ----------
export interface ChatChunk {
  text?: string;
  tool?: string;
  args?: any;
  result?: any;
  draft_id?: string;
}

/** SSE 流式对话. onChunk 收到每个 data: 事件; 返回时流已结束 ([DONE]). */
export async function chatStream(
  payload: { message: string; draft_id?: string | null; asset_paths?: string[] },
  onChunk: (c: ChatChunk) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  });
  if (!resp.body) throw new Error('no response body');

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const chunk = line.slice(6);
      if (chunk === '[DONE]') return;
      try { onChunk(JSON.parse(chunk)); }
      catch { onChunk({ text: chunk }); }
    }
  }
}

// ---------- LocalSend ----------
export function localsendStatus(): Promise<LocalSendStatus> {
  return jsonFetch<LocalSendStatus>('/api/localsend/status');
}
export function localsendStart(): Promise<any> {
  return jsonFetch('/api/localsend/start', { method: 'POST' });
}
export function localsendStop(): Promise<any> {
  return jsonFetch('/api/localsend/stop', { method: 'POST' });
}

// ---------- 系统 ----------
export function health(): Promise<{ ok: boolean }> {
  return jsonFetch('/health');
}
