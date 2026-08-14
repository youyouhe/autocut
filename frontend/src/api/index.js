import axios from 'axios'

const api = axios.create({ timeout: 600000 })

export default {
  // ===== 资源 =====
  upload: (formData, onProgress) => api.post('/api/upload', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    onUploadProgress: onProgress
  }),
  analyze: (formData) => api.post('/perceive/video', formData, {
    headers: { 'Content-Type': 'multipart/form-data' }, timeout: 120000
  }),
  analyzeByPath: (filePath) => api.post('/api/perceive', { path: filePath }, { timeout: 120000 }),
  checkCached: (filePath) => api.get('/api/perceive/cached', { params: { path: filePath } }),

  // ===== 编辑 =====
  createDraft: (width = 1080, height = 1920) =>
    api.post('/create_draft', { width, height }),
  addVideo: (data) => api.post('/add_video', data),
  addText: (data) => api.post('/add_text', data),
  addAudio: (data) => api.post('/add_audio', data),
  addImage: (data) => api.post('/add_image', data),
  saveDraft: (data) => api.post('/save_draft', data),

  // ===== 渲染 =====
  render: (draftId) => api.post(`/render/draft/${draftId}`),
  renderStatus: (taskId) => api.get(`/render/status/${taskId}`),
  renderList: () => api.get('/render/list'),
  renderDownload: (taskId) => `/render/download/${taskId}`,

  // ===== 对话 (SSE 流式) =====
  chat: async (data, onChunk) => {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    })
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop()
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const chunk = line.slice(6)
          if (chunk === '[DONE]') return
          try { onChunk(JSON.parse(chunk)) } catch { onChunk({ text: chunk }) }
        }
      }
    }
  },

  // ===== 模板 =====
  listTemplates: () => api.get('/api/templates'),
  renderTemplate: (data) => api.post('/api/templates/render', data),

  // ===== LocalSend (手机/电脑直接发素材) =====
  localsendStatus: () => api.get('/api/localsend/status'),
  localsendStart: () => api.post('/api/localsend/start'),
  localsendStop: () => api.post('/api/localsend/stop'),
  scanAssets: () => api.get('/api/assets'),

  // ===== 系统 =====
  health: () => api.get('/health'),
}
