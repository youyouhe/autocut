<template>
  <el-card class="draft-panel" body-style="padding:0;height:100%;display:flex;flex-direction:column;overflow:hidden;">
    <template #header>
      <div class="panel-header">
        <span>🎞️ 草稿管理</span>
        <el-button size="small" @click="loadDrafts" :loading="loading">刷新</el-button>
      </div>
    </template>

    <div class="draft-grid scrollbar-thin">
      <div v-for="d in drafts" :key="d.id" class="draft-card"
        :class="{ 'draft-active': selectedDraft === d.folder }"
        @click="selectDraft(d)">
        <!-- 封面 -->
        <div class="draft-cover">
          <img v-if="d.cover_url" :src="d.cover_url" loading="lazy" @error="onCoverError(d)" />
          <div v-else class="cover-placeholder">
            <el-icon :size="32"><Film /></el-icon>
          </div>
          <div class="draft-duration">{{ formatDuration(d.duration) }}</div>
          <div class="draft-type-tag" v-if="d.type">{{ d.type }}</div>
        </div>
        <!-- 信息 -->
        <div class="draft-info">
          <div class="draft-name" :title="d.name">{{ d.name }}</div>
          <div class="draft-meta">
            <span>{{ formatSize(d.size_bytes) }}</span>
            <span>{{ formatTime(d.modified) }}</span>
          </div>
        </div>
        <!-- 操作 -->
        <div class="draft-actions" @click.stop>
          <el-button size="small" type="primary" @click="renderDraft(d)">渲染</el-button>
          <el-button size="small" type="danger" plain circle @click="deleteDraft(d)">
            <el-icon><Delete /></el-icon>
          </el-button>
        </div>
      </div>

      <div v-if="!loading && drafts.length === 0" class="empty-drafts">
        <el-icon :size="48" color="#c0c4cc"><Film /></el-icon>
        <p>暂无草稿</p>
        <p class="empty-hint">在对话中创建草稿，或上传 zip 导入</p>
      </div>
    </div>
  </el-card>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import api from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useChatStore } from '../stores/chat'
import { useRenderStore } from '../stores/render'

const chatStore = useChatStore()
const renderStore = useRenderStore()

const drafts = ref([])
const loading = ref(false)
const selectedDraft = ref(null)

onMounted(() => loadDrafts())

async function loadDrafts() {
  loading.value = true
  try {
    const { data } = await api.get('/api/drafts')
    drafts.value = data
  } catch (e) {
    ElMessage.error('加载草稿失败')
  } finally {
    loading.value = false
  }
}

function selectDraft(d) {
  selectedDraft.value = d.folder
}

async function renderDraft(d) {
  try {
    const { data } = await api.post(`/render/draft/${d.id}`)
    if (data.task_id) {
      renderStore.tasks.unshift({ task_id: data.task_id, status: 'queued', draft_id: d.id, draft: d.name, created: Date.now() })
      renderStore.startPolling()
      ElMessage.success(`已提交渲染: ${d.name}`)
    }
  } catch (e) {
    ElMessage.error('渲染失败: ' + (e.response?.data?.error || e.message))
  }
}

async function deleteDraft(d) {
  try {
    await ElMessageBox.confirm(`删除草稿「${d.name}」？此操作不可恢复。`, '删除确认', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消'
    })
    await api.delete(`/api/drafts/${d.folder}`)
    drafts.value = drafts.value.filter(x => x.folder !== d.folder)
    ElMessage.success('已删除')
  } catch {}
}

function onCoverError(d) { d.cover_url = null }

function formatDuration(s) {
  if (!s) return '0s'
  const m = Math.floor(s / 60)
  const sec = Math.round(s % 60)
  return m > 0 ? `${m}:${String(sec).padStart(2, '0')}` : `${sec}s`
}

function formatSize(bytes) {
  if (!bytes) return ''
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}KB`
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`
}

function formatTime(us) {
  if (!us) return ''
  const d = new Date(us * 1000)
  return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`
}
</script>

<style scoped>
.draft-panel { background: var(--bg-card); border: 1px solid var(--border-color); }
.panel-header { display: flex; align-items: center; justify-content: space-between; font-weight: 600; }

.draft-grid {
  flex: 1; overflow-y: auto; padding: 10px;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px; align-content: start;
}

.draft-card {
  border: 1px solid var(--border-color); border-radius: 10px; overflow: hidden;
  background: var(--bg-card); cursor: pointer; transition: all 0.25s; position: relative;
}
.draft-card:hover { border-color: var(--accent); box-shadow: 0 4px 16px rgba(64,158,255,0.12); transform: translateY(-2px); }
.draft-active { border-color: var(--accent) !important; box-shadow: 0 0 0 2px rgba(64,158,255,0.3); }

.draft-cover { width: 100%; aspect-ratio: 16/9; background: var(--bg-input); position: relative; overflow: hidden; }
.draft-cover img { width: 100%; height: 100%; object-fit: cover; }
.cover-placeholder { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: #c0c4cc; }

.draft-duration {
  position: absolute; bottom: 6px; right: 6px; font-size: 11px; color: #fff;
  background: rgba(0,0,0,0.75); padding: 2px 6px; border-radius: 4px;
}
.draft-type-tag {
  position: absolute; top: 6px; left: 6px; font-size: 10px; color: #fff;
  background: rgba(64,158,255,0.85); padding: 1px 6px; border-radius: 3px;
}

.draft-info { padding: 8px 10px; }
.draft-name { font-size: 13px; color: var(--text-primary); font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 4px; }
.draft-meta { font-size: 11px; color: var(--text-secondary); display: flex; gap: 8px; }

.draft-actions {
  position: absolute; top: 6px; right: 6px; display: flex; gap: 4px; opacity: 0; transition: opacity 0.2s;
}
.draft-card:hover .draft-actions { opacity: 1; }

.empty-drafts { grid-column: 1/-1; text-align: center; padding: 50px 0; color: var(--text-secondary); }
.empty-drafts p { margin-top: 8px; font-size: 13px; }
.empty-hint { font-size: 11px !important; color: #c0c4cc; }
</style>
