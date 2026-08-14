import { defineStore } from 'pinia'
import api from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

export const useProjectStore = defineStore('project', {
  state: () => ({
    currentDraftId: null,      // 当前编辑中的草稿 ID
    currentDraftName: '',      // 当前草稿名称
    currentScenes: [],         // 当前草稿的场景列表 (编辑时间线)
    isDirty: false,            // 有未保存的修改
    savedDrafts: [],          // 已保存的草稿列表 (磁盘上的)
  }),

  getters: {
    hasUnsavedChanges: (s) => s.isDirty && s.currentDraftId,
    hasCurrentDraft: (s) => !!s.currentDraftId,
  },

  actions: {
    async newDraft(name, width = 1080, height = 1920) {
      // 检查未保存
      if (this.isDirty) {
        try {
          await ElMessageBox.confirm(
            '当前草稿有未保存的修改，是否保存？',
            '提示',
            { confirmButtonText: '保存', cancelButtonText: '丢弃', type: 'warning' }
          )
          await this.saveDraft()
        } catch {
          // 用户选择丢弃
        }
      }

      // 创建新草稿
      const { data } = await api.createDraft(width, height)
      if (data.success) {
        this.currentDraftId = data.output.draft_id
        this.currentDraftName = name || `草稿_${data.output.draft_id.slice(-6)}`
        this.currentScenes = []
        this.isDirty = false
        ElMessage.success(`新草稿: ${this.currentDraftName}`)
      }
      return this.currentDraftId
    },

    async loadDraft(draftId, name) {
      // 检查未保存
      if (this.isDirty) {
        try {
          await ElMessageBox.confirm(
            '当前草稿有未保存的修改，是否保存？',
            '切换草稿',
            { confirmButtonText: '保存并切换', cancelButtonText: '直接切换', type: 'warning' }
          )
          await this.saveDraft()
        } catch {}
      }

      this.currentDraftId = draftId
      this.currentDraftName = name || draftId
      this.currentScenes = []
      this.isDirty = false
      ElMessage.info(`已加载草稿: ${this.currentDraftName}`)
    },

    markDirty() {
      this.isDirty = true
    },

    async addVideo(videoUrl, opts = {}) {
      if (!this.currentDraftId) return
      const { data } = await api.addVideo({
        draft_id: this.currentDraftId,
        video_url: videoUrl,
        start: opts.start || 0,
        end: opts.end,
        target_start: opts.target_start || 0,
        volume: opts.volume,
        transition: opts.transition,
      })
      this.isDirty = true
      this.currentScenes.push({ type: 'video', ...opts })
      return data
    },

    async addText(text, opts = {}) {
      if (!this.currentDraftId) return
      const { data } = await api.addText({
        draft_id: this.currentDraftId,
        text,
        start: opts.start || 0,
        end: opts.end || 5,
        font_size: opts.font_size || 12,
        font_color: opts.font_color || '#FFFFFF',
        transform_y: opts.transform_y || 0,
        transform_x: opts.transform_x || 0,
      })
      this.isDirty = true
      this.currentScenes.push({ type: 'text', text, ...opts })
      return data
    },

    async saveDraft() {
      if (!this.currentDraftId) return
      const { data } = await api.saveDraft({ draft_id: this.currentDraftId })
      if (data.success) {
        this.isDirty = false
        ElMessage.success(`草稿已保存: ${this.currentDraftName}`)
        await this.loadSavedDrafts()
      }
      return data
    },

    async loadSavedDrafts() {
      try {
        const { data } = await api.get('/api/drafts')
        this.savedDrafts = data
      } catch {}
    },

    async deleteDraft(folder) {
      try {
        await ElMessageBox.confirm(`删除草稿「${folder}」？`, '确认', { type: 'warning' })
        await api.delete(`/api/drafts/${folder}`)
        this.savedDrafts = this.savedDrafts.filter(d => d.folder !== folder)
        if (this.currentDraftName === folder) {
          this.currentDraftId = null
          this.currentDraftName = ''
          this.currentScenes = []
          this.isDirty = false
        }
        ElMessage.success('已删除')
      } catch {}
    },

    async renderCurrent() {
      if (!this.currentDraftId) {
        ElMessage.warning('没有当前草稿')
        return null
      }
      if (this.isDirty) {
        try {
          await ElMessageBox.confirm(
            '有未保存的修改，需要先保存才能渲染。是否保存？',
            '渲染前保存',
            { confirmButtonText: '保存并渲染', cancelButtonText: '取消', type: 'warning' }
          )
          await this.saveDraft()
        } catch {
          return null
        }
      }
      const { data } = await api.render(this.currentDraftId)
      if (data.task_id) {
        ElMessage.success(`渲染已提交: ${data.task_id}`)
        return data.task_id
      }
      return null
    },

    closeDraft() {
      this.currentDraftId = null
      this.currentDraftName = ''
      this.currentScenes = []
      this.isDirty = false
    },
  }
})
