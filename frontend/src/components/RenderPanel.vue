<template>
  <el-card class="render-panel" body-style="padding:12px;height:100%;display:flex;flex-direction:column;overflow:hidden;">
    <template #header>
      <div class="panel-header">
        <span>🎬 渲染任务</span>
        <el-button size="small" @click="store.refreshList()" :loading="false">刷新</el-button>
      </div>
    </template>

    <div class="task-list scrollbar-thin">
      <div v-for="task in store.tasks" :key="task.task_id" class="task-card">
        <div class="task-header">
          <span class="task-id">{{ task.task_id }}</span>
          <el-tag :type="statusType(task.status)" size="small">{{ statusLabel(task.status) }}</el-tag>
        </div>
        <div class="task-info">
          <span v-if="task.draft_name || task.draft">{{ task.draft_name || task.draft }}</span>
          <span v-if="task.duration" class="task-duration">{{ task.duration.toFixed(0) }}s</span>
        </div>
        <div v-if="task.mp4_name" class="task-output">
          <el-icon><VideoPlay /></el-icon> {{ task.mp4_name }}
        </div>
        <div v-if="task.status === 'done'" class="task-actions">
          <a :href="store.downloadUrl(task.task_id)" download>
            <el-button size="small" type="primary">⬇ 下载</el-button>
          </a>
        </div>
        <div v-if="task.status === 'error'" class="task-error">
          {{ (task.error || '').slice(0, 100) }}
        </div>
      </div>
      <div v-if="store.tasks.length === 0" class="empty-tip">
        暂无渲染任务<br>
        <span class="empty-hint">先创建草稿，再点渲染</span>
      </div>
    </div>

    <div class="render-status" v-if="store.polling">
      <el-icon class="is-loading"><Loading /></el-icon>
      正在渲染...
    </div>
  </el-card>
</template>

<script setup>
import { useRenderStore } from '../stores/render'
const store = useRenderStore()

function statusType(s) {
  return { done: 'success', error: 'danger', rendering: 'warning', queued: 'info' }[s] || ''
}
function statusLabel(s) {
  return { done: '✅ 完成', error: '❌ 失败', rendering: '🔄 渲染中', queued: '⏳ 排队' }[s] || s
}
</script>

<style scoped>
.render-panel { background: var(--bg-card); border: 1px solid #333; }
.panel-header { display: flex; align-items: center; justify-content: space-between; font-weight: 600; }
.task-list { flex: 1; overflow-y: auto; }
.task-card {
  border: 1px solid #333; border-radius: 6px; padding: 10px; margin-bottom: 8px;
  background: rgba(255,255,255,0.03);
}
.task-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
.task-id { font-size: 11px; color: var(--text-secondary); font-family: monospace; }
.task-info { font-size: 12px; color: var(--text-primary); display: flex; justify-content: space-between; }
.task-duration { color: var(--accent); }
.task-output { font-size: 12px; color: var(--accent-warm); margin-top: 4px; display: flex; align-items: center; gap: 4px; }
.task-actions { margin-top: 8px; }
.task-error { font-size: 11px; color: var(--danger); margin-top: 4px; word-break: break-all; }
.empty-tip { text-align: center; color: var(--text-secondary); padding: 40px 0; font-size: 13px; }
.empty-hint { font-size: 11px; }
.render-status { padding: 8px; text-align: center; font-size: 13px; color: var(--accent); }
.render-status .el-icon { margin-right: 4px; }
a { text-decoration: none; }
</style>
