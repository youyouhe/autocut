import { defineStore } from 'pinia'
import api from '../api'
import { useAssetStore } from './asset'
import { useRenderStore } from './render'
import { marked } from 'marked'

export const useChatStore = defineStore('chat', {
  state: () => ({
    messages: [],
    draftId: null,
    sending: false,
    inputText: '',
  }),
  actions: {
    async send() {
      const text = this.inputText.trim()
      if (!text || this.sending) return
      this.inputText = ''
      this.messages.push({ role: 'user', content: text })
      this.sending = true

      const aiMsg = { role: 'assistant', content: '', actions: [] }
      this.messages.push(aiMsg)

      const assetStore = useAssetStore()
      // 传资源路径列表给后端，后端从内存缓存查完整分析（VLM+ASR）
      const assetPaths = assetStore.assets.map(a => a.path)

      try {
        await api.chat(
          { message: text, draft_id: this.draftId, asset_paths: assetPaths },
          (chunk) => {
            if (chunk.text) aiMsg.content += chunk.text
            if (chunk.action_result) aiMsg.actions.push(chunk.action_result)
            if (chunk.draft_id) this.draftId = chunk.draft_id
          }
        )
        aiMsg.html = marked(aiMsg.content)
      } catch (e) {
        aiMsg.content = `❌ 对话失败: ${e.message}`
        aiMsg.html = `<p style="color:var(--danger)">❌ 对话失败: ${e.message}</p>`
      } finally {
        this.sending = false
      }
    },

    async quickCreate() {
      const { data } = await api.createDraft(1080, 1920)
      if (data.success) {
        this.draftId = data.output.draft_id
        this.messages.push({ role: 'assistant', content: `草稿已创建: ${this.draftId}` })
      }
    },

    async quickRender() {
      if (!this.draftId) return
      const renderStore = useRenderStore()
      const task = await renderStore.submit(this.draftId)
      if (task) {
        this.messages.push({ role: 'assistant', content: `渲染已提交: ${task}` })
      }
    },

    clear() {
      this.messages = []
      this.draftId = null
    }
  }
})
