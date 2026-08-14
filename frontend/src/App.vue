<template>
  <div class="app-container">
    <el-header class="app-header">
      <div class="header-left">
        <span class="logo">🎬</span>
        <span class="title">AI 视频编辑工作台</span>
      </div>

      <!-- 全局草稿操作 -->
      <div class="header-center">
        <el-button type="primary" size="small" @click="handleNewDraft">
          <el-icon><Plus /></el-icon> 新建草稿
        </el-button>

        <el-dropdown v-if="project.hasCurrentDraft" trigger="click" @command="handleDraftCommand">
          <el-button size="small">
            <el-icon><Document /></el-icon>
            {{ project.currentDraftName }}
            <el-badge v-if="project.isDirty" is-dot class="dirty-dot" />
            <el-icon><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="save" :disabled="!project.isDirty">
                <el-icon><Save /></el-icon> 保存草稿
              </el-dropdown-item>
              <el-dropdown-item command="render">
                <el-icon><VideoPlay /></el-icon> 渲染
              </el-dropdown-item>
              <el-dropdown-item divided command="close">
                <el-icon><Close /></el-icon> 关闭草稿
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>

        <el-button size="small" plain @click="showDraftList = true">
          <el-icon><FolderOpened /></el-icon> 草稿库
        </el-button>
      </div>

      <div class="header-right">
        <el-tag :type="serverOnline ? 'success' : 'danger'" size="small">{{ serverOnline ? '在线' : '离线' }}</el-tag>
        <el-tag v-if="project.hasCurrentDraft" type="info" size="small">
          {{ project.isDirty ? '未保存' : '已保存' }}
        </el-tag>
      </div>
    </el-header>

    <!-- 草稿名称输入对话框 -->
    <el-dialog v-model="showNewDraft" title="新建草稿" width="400px">
      <el-form label-width="80px">
        <el-form-item label="草稿名">
          <el-input v-model="newDraftName" placeholder="如：产品介绍视频" @keydown.enter="confirmNewDraft" />
        </el-form-item>
        <el-form-item label="画布">
          <el-radio-group v-model="newDraftCanvas">
            <el-radio value="1080x1920">1080×1920 (竖屏)</el-radio>
            <el-radio value="1920x1080">1920×1080 (横屏)</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showNewDraft = false">取消</el-button>
        <el-button type="primary" @click="confirmNewDraft">创建</el-button>
      </template>
    </el-dialog>

    <!-- 草稿库弹窗 -->
    <el-dialog v-model="showDraftList" title="草稿库" width="700px" @open="project.loadSavedDrafts()">
      <div class="draft-browser">
        <div v-for="d in project.savedDrafts" :key="d.id" class="browser-item"
          @click="openSavedDraft(d)">
          <div class="browser-cover">
            <img v-if="d.cover_url" :src="d.cover_url" loading="lazy" />
            <div v-else class="browser-no-cover"><el-icon><Film /></el-icon></div>
            <div class="browser-dur">{{ formatDur(d.duration) }}</div>
          </div>
          <div class="browser-info">
            <div class="browser-name">{{ d.name }}</div>
            <div class="browser-meta">{{ formatSize(d.size_bytes) }} · {{ formatTime(d.modified) }}</div>
          </div>
          <el-button size="small" type="danger" plain circle @click.stop="project.deleteDraft(d.folder)">
            <el-icon><Delete /></el-icon>
          </el-button>
        </div>
        <div v-if="project.savedDrafts.length === 0" class="browser-empty">
          暂无已保存的草稿
        </div>
      </div>
    </el-dialog>

    <el-main class="app-main">
      <div class="main-row">
        <div class="panel-col" :style="{ flex: col1 + ' 1 0' }">
          <AssetPanel v-if="activeView === 'assets'" />
          <DraftPanel v-if="activeView === 'drafts'" />
        </div>
        <div class="resizer" @mousedown="startResize($event, 1)"></div>
        <div class="panel-col" :style="{ flex: col2 + ' 1 0' }"><ChatPanel /></div>
        <div class="resizer" @mousedown="startResize($event, 2)"></div>
        <div class="panel-col" :style="{ flex: col3 + ' 1 0' }"><RenderPanel /></div>
      </div>
    </el-main>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useProjectStore } from './stores/project'
import { useChatStore } from './stores/chat'
import { useRenderStore } from './stores/render'
import api from './api'
import AssetPanel from './components/AssetPanel.vue'
import ChatPanel from './components/ChatPanel.vue'
import RenderPanel from './components/RenderPanel.vue'
import DraftPanel from './components/DraftPanel.vue'

const project = useProjectStore()
const chatStore = useChatStore()
const renderStore = useRenderStore()

const serverOnline = ref(false)
const activeView = ref('assets')
const showNewDraft = ref(false)
const showDraftList = ref(false)
const newDraftName = ref('')
const newDraftCanvas = ref('1080x1920')
let healthTimer = null

// === 动态布局: 三栏 flex 比例 (12 : 7 : 5), 可拖拽分隔条调整 ===
const col1 = ref(12)
const col2 = ref(7)
const col3 = ref(5)

function startResize(e, which) {
  e.preventDefault()
  const startX = e.clientX
  const total = col1.value + col2.value + col3.value
  const startC1 = col1.value
  const startC2 = col2.value
  const startC3 = col3.value
  const containerWidth = e.target.parentElement.offsetWidth
  const unit = containerWidth / total  // 每份对应像素

  function onMove(ev) {
    const dx = ev.clientX - startX
    const dUnits = dx / unit
    if (which === 1) {
      // 拖第1条分隔: col1 <-> col2
      let c1 = startC1 + dUnits
      let c2 = startC2 - dUnits
      if (c1 < 3) { c1 = 3; c2 = startC1 + startC2 - 3 }
      if (c2 < 3) { c2 = 3; c1 = startC1 + startC2 - 3 }
      col1.value = c1
      col2.value = c2
    } else if (which === 2) {
      // 拖第2条分隔: col2 <-> col3
      let c2 = startC2 + dUnits
      let c3 = startC3 - dUnits
      if (c2 < 3) { c2 = 3; c3 = startC2 + startC3 - 3 }
      if (c3 < 3) { c3 = 3; c2 = startC2 + startC3 - 3 }
      col2.value = c2
      col3.value = c3
    }
  }
  function onUp() {
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
  }
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
}

onMounted(async () => {
  await checkHealth()
  healthTimer = setInterval(checkHealth, 10000)
  await project.loadSavedDrafts()
  await renderStore.refreshList()
  if (renderStore.tasks.some(t => t.status === 'queued' || t.status === 'rendering')) {
    renderStore.startPolling()
  }
})
onUnmounted(() => { if (healthTimer) clearInterval(healthTimer); renderStore.stopPolling() })

async function checkHealth() { try { await api.health(); serverOnline.value = true } catch { serverOnline.value = false } }

function handleNewDraft() { showNewDraft.value = true; newDraftName.value = '' }

async function confirmNewDraft() {
  const [w, h] = newDraftCanvas.value.split('x').map(Number)
  const id = await project.newDraft(newDraftName.value || '', w, h)
  chatStore.draftId = id
  showNewDraft.value = false
}

async function handleDraftCommand(cmd) {
  if (cmd === 'save') await project.saveDraft()
  else if (cmd === 'render') {
    const taskId = await project.renderCurrent()
    if (taskId) { renderStore.tasks.unshift({task_id:taskId,status:'queued',created:Date.now()}); renderStore.startPolling() }
  }
  else if (cmd === 'close') project.closeDraft()
}

async function openSavedDraft(d) {
  await project.loadDraft(d.id, d.name)
  chatStore.draftId = d.id
  showDraftList.value = false
}

function formatDur(s) { const m=Math.floor(s/60),x=Math.round(s%60); return m>0?`${m}:${String(x).padStart(2,'0')}`:`${x}s` }
function formatSize(b) { if(!b) return ''; return b<1048576 ? `${(b/1024).toFixed(0)}KB` : `${(b/1048576).toFixed(1)}MB` }
function formatTime(us) { if(!us) return ''; const d=new Date(us*1000); return `${d.getMonth()+1}/${d.getDate()}` }
</script>

<style scoped>
.app-container { height: 100vh; display: flex; flex-direction: column; background: var(--bg-dark); }
.app-header { display: flex; align-items: center; justify-content: space-between; background: var(--bg-card); border-bottom: 1px solid var(--border-color); padding: 0 16px; height: 50px; }
.header-left { display: flex; align-items: center; gap: 8px; }
.logo { font-size: 20px; }
.title { font-size: 16px; font-weight: 600; color: var(--text-primary); }
.header-center { display: flex; align-items: center; gap: 8px; }
.dirty-dot { margin-left: 4px; }
.header-right { display: flex; gap: 6px; }
.app-main { flex: 1; overflow: hidden; padding: 6px; }
.main-row { height: 100%; margin: 0 !important; display: flex; align-items: stretch; gap: 0; }
.panel-col { height: 100%; overflow: hidden; min-width: 0; }
.panel-col > :deep(*) { height: 100%; }
.resizer {
  width: 6px; cursor: col-resize; flex: 0 0 6px; position: relative;
  z-index: 5; transition: background 0.15s;
}
.resizer:hover, .resizer:active { background: var(--accent); border-radius: 3px; }

/* 草稿库弹窗 */
.draft-browser { display: flex; flex-direction: column; gap: 8px; max-height: 500px; overflow-y: auto; }
.browser-item { display: flex; align-items: center; gap: 12px; padding: 10px; border: 1px solid var(--border-color); border-radius: 8px; cursor: pointer; transition: all 0.2s; }
.browser-item:hover { border-color: var(--accent); background: rgba(64,158,255,0.05); }
.browser-cover { width: 80px; height: 45px; border-radius: 4px; overflow: hidden; background: var(--bg-input); position: relative; flex-shrink: 0; }
.browser-cover img { width: 100%; height: 100%; object-fit: cover; }
.browser-no-cover { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: #c0c4cc; }
.browser-dur { position: absolute; bottom: 2px; right: 2px; font-size: 10px; color: #fff; background: rgba(0,0,0,0.7); padding: 1px 4px; border-radius: 2px; }
.browser-info { flex: 1; }
.browser-name { font-size: 14px; color: var(--text-primary); font-weight: 500; }
.browser-meta { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
.browser-empty { text-align: center; padding: 40px; color: var(--text-secondary); }
</style>
