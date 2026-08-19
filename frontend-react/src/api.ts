// 真实后端 API 封装 — 全部指向 Flask render_server (:9010, 同源).
// 端点契约见 SYSTEM_MANUAL.md §16 与 render_server.py / capcut_server.py.

// ---------- 类型 (按真实返回字段) ----------
export type AssetType = 'video' | 'image' | 'audio' | 'subtitle' | 'text' | 'other';

export interface Asset {
  name: string;
  path: string;
  file: string;
  type: AssetType;
  size?: number;
  modified_at?: string;
  has_audio?: boolean | null;
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
  tags?: string[];   // VLM 抽出的短关键词, 供 agent 快速按标签检索
  audio?: { full_text?: string; segments?: any[]; asr_model?: string };
  srt?: string;   // 标准 SRT 字幕文本
  analysis_mode?: 'asr' | 'vlm' | 'none';   // 本次内容判断走的是听(asr)还是看(vlm)
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

export function deleteAsset(name: string): Promise<{ ok: boolean; error?: string }> {
  return jsonFetch(`/api/assets/${encodeURIComponent(name)}`, { method: 'DELETE' });
}

/** 去除视频音轨 —— 之后该素材没有音轨, 分析时自动跳过 VAD/ASR, 只用画面(VLM)匹配,
 * 不再受环境音/嘈杂人声被误判成"语音"的影响。 */
export function stripAudio(name: string): Promise<{ ok: boolean; error?: string; name?: string; has_audio?: boolean }> {
  return jsonFetch(`/api/assets/${encodeURIComponent(name)}/strip-audio`, { method: 'POST' });
}

export function perceive(path: string, opts?: { force?: boolean; do_asr?: boolean; frames?: number }): Promise<PerceiveResult> {
  return jsonFetch<PerceiveResult>('/api/perceive', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, force: opts?.force, do_asr: opts?.do_asr ?? true, frames: opts?.frames ?? 4 }),
  });
}

export interface Shot {
  index: number;
  start: number;
  end: number;
  duration: number;
  clip_path?: string | null;
  keyframe_path?: string | null;
  clip_url?: string | null;
  keyframe_url?: string | null;
}

/** 只读分镜拆分缓存, 没拆过 shots 是 null */
export function getShots(name: string): Promise<{ shots: Shot[] | null }> {
  return jsonFetch(`/api/assets/${encodeURIComponent(name)}/shots`);
}

/** 分镜拆分: GPU CNN 特征检测镜头边界, 按边界切出每个镜头的独立小视频+关键帧 */
export function splitShots(name: string, opts?: { force?: boolean; sample_fps?: number; min_scene_len_sec?: number }): Promise<{ ok: boolean; error?: string; shots?: Shot[] }> {
  return jsonFetch(`/api/assets/${encodeURIComponent(name)}/split-shots`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ force: opts?.force, sample_fps: opts?.sample_fps ?? 5, min_scene_len_sec: opts?.min_scene_len_sec ?? 0.6 }),
  });
}

export function checkCached(path: string): Promise<{ cached: boolean; result?: PerceiveResult }> {
  return jsonFetch(`/api/perceive/cached?path=${encodeURIComponent(path)}`);
}

export interface MainVideo {
  path: string;
  name: string;
  set_at: number;
  url?: string;
  poster_path?: string;
  poster_url?: string;
}

/** 当前"主视频"(最新录制的那条, 跟长期存在的素材库分开管理) */
export function getMainVideo(): Promise<{ main_video: MainVideo | null }> {
  return jsonFetch('/api/main-video');
}

/** 标记为当前主视频; 旧的主视频自动变回普通素材库的一条 */
export function setMainVideo(name: string): Promise<{ ok: boolean; error?: string; main_video?: MainVideo }> {
  return jsonFetch(`/api/assets/${encodeURIComponent(name)}/set-main`, { method: 'POST' });
}

export function clearMainVideo(): Promise<{ ok: boolean }> {
  return jsonFetch('/api/main-video/clear', { method: 'POST' });
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

// ---------- 模板 ----------
export interface TemplateInfo {
  file: string;
  name: string;
  description: string;
  variables: string[];
}
export function listTemplates(): Promise<TemplateInfo[]> {
  return jsonFetch<TemplateInfo[]>('/api/templates');
}

export interface TemplateRenderResult {
  draft_id?: string;
  save?: { success?: boolean; error?: string };
  render?: { task_id?: string };
  error?: string;
}
export function renderTemplate(templateFile: string, variables: Record<string, string>, doRender = false): Promise<TemplateRenderResult> {
  const template = templateFile.replace(/\.ya?ml$/, '');
  return jsonFetch<TemplateRenderResult>('/api/templates/render', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ template, variables, render: doRender }),
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
export interface ChatMessage {
  role: 'user' | 'assistant' | 'tool';
  content: string;
  toolDetails?: { tool: string; args: any; result: any };
}

export interface ChatChunk {
  text?: string;
  tool?: string;
  args?: any;
  result?: any;
  draft_id?: string;
  conversation_id?: string;
}

/** SSE 流式对话. onChunk 收到每个 data: 事件; 返回时流已结束 ([DONE]). */
export async function chatStream(
  payload: { message: string; draft_id?: string | null; asset_paths?: string[]; conversation_id?: string | null },
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

export interface ConversationSummary {
  id: string;
  title: string | null;
  draft_id: string | null;
  created_at: number;
  updated_at: number;
  message_count: number;
}
export interface Conversation {
  id: string;
  title: string | null;
  draft_id: string | null;
  messages: ChatMessage[];
  created_at: number;
  updated_at: number;
}

export function listConversations(): Promise<ConversationSummary[]> {
  return jsonFetch<ConversationSummary[]>('/api/chat/conversations');
}
export function createConversation(draft_id?: string | null): Promise<{ id: string }> {
  return jsonFetch('/api/chat/conversations', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ draft_id: draft_id ?? null }),
  });
}
export function getConversation(id: string): Promise<Conversation> {
  return jsonFetch<Conversation>(`/api/chat/conversations/${encodeURIComponent(id)}`);
}
export function deleteConversation(id: string): Promise<{ ok: boolean }> {
  return jsonFetch(`/api/chat/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
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

// ---------- 设置 (LLM/ASR 密钥) ----------
export interface SettingField {
  key: string;
  label: string;
  secret: boolean;
  group: 'llm' | 'asr' | 'analysis';
  type: 'text' | 'secret' | 'bool';
  value: string | boolean;   // secret 字段为脱敏值 (如 ****ab12), bool 字段为 true/false, 仅供展示
  configured: boolean;
}
export function getSettings(): Promise<SettingField[]> {
  return jsonFetch<SettingField[]>('/api/settings');
}
export function saveSettings(values: Record<string, string | boolean>): Promise<SettingField[]> {
  return jsonFetch<SettingField[]>('/api/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(values),
  });
}
export interface TestResult { ok: boolean; error?: string; model?: string; status?: number }
export function testSetting(target: 'llm' | 'asr', overrides: Record<string, string | boolean>): Promise<TestResult> {
  return jsonFetch<TestResult>('/api/settings/test', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target, ...overrides }),
  });
}
