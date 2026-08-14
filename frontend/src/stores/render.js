import { defineStore } from 'pinia'
import api from '../api'

export const useRenderStore = defineStore('render', {
  state: () => ({
    tasks: [],
    polling: false,
    pollTimer: null,
  }),
  actions: {
    async submit(draftId) {
      const { data } = await api.render(draftId)
      if (data.task_id) {
        this.tasks.unshift({
          task_id: data.task_id,
          status: 'queued',
          draft_id: draftId,
          created: Date.now(),
        })
        this.startPolling()
        return data.task_id
      }
      return null
    },

    startPolling() {
      if (this.pollTimer) return
      this.polling = true
      this.pollTimer = setInterval(() => this.pollAll(), 5000)
    },

    stopPolling() {
      if (this.pollTimer) {
        clearInterval(this.pollTimer)
        this.pollTimer = null
        this.polling = false
      }
    },

    async pollAll() {
      const active = this.tasks.filter(t => t.status === 'queued' || t.status === 'rendering')
      if (active.length === 0) { this.stopPolling(); return }
      for (const t of active) {
        try {
          const { data } = await api.renderStatus(t.task_id)
          Object.assign(t, data)
        } catch {}
      }
    },

    async refreshList() {
      try {
        const { data } = await api.renderList()
        this.tasks = data || []
      } catch {}
    },

    downloadUrl(taskId) {
      return api.renderDownload(taskId)
    }
  }
})
