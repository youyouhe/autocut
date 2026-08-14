<template>
  <el-dialog v-model="visible" title="接收素材 (LocalSend)" width="560px"
    :close-on-click-modal="false" @close="onClose" class="receive-dialog">
    <!-- 顶部状态 -->
    <div class="recv-status">
      <div class="recv-device">
        <el-icon :size="22"><Cellphone /></el-icon>
        <div>
          <div class="recv-alias">{{ status.alias || 'AI 视频工作台' }}</div>
          <div class="recv-port">本机IP {{ status.my_ip || '?' }} · 端口 {{ status.port || 53317 }}</div>
        </div>
      </div>
      <el-tag :type="status.active_sender ? 'warning' : 'success'" size="small" effect="dark">
        {{ status.active_sender ? `正在接收: ${status.active_sender}` : '等待连接…' }}
      </el-tag>
    </div>

    <!-- 指引 -->
    <el-alert type="info" :closable="false" class="recv-guide">
      <template #title>
        方法1(推荐): 手机/电脑打开 <b>LocalSend</b> App → 选「发送」→ 附近设备自动出现
        <b>「{{ status.alias || 'AI 视频工作台' }}」</b>
      </template>
    </el-alert>
    <el-alert type="warning" :closable="false" class="recv-guide">
      <template #title>
        方法2(搜不到时): 手机 LocalSend → 设置 → 「附近设备」→ 右上角 + 手动添加 →
        输入 <b>{{ status.my_ip || '本机IP' }}:53317</b>
      </template>
    </el-alert>

    <!-- 进行中 -->
    <div v-if="pendingItems.length" class="recv-section">
      <div class="recv-section-title">传输中 ({{ pendingItems.length }})</div>
      <div v-for="(f, i) in pendingItems" :key="'p'+i" class="recv-row pending">
        <span class="recv-icon">{{ typeIcon(f.fileType) }}</span>
        <div class="recv-info">
          <div class="recv-fname">{{ f.fileName }}</div>
          <div class="recv-meta">{{ formatSize(f.size) }}</div>
        </div>
        <el-icon class="is-loading recv-spin"><Loading /></el-icon>
        <span class="recv-state">传输中…</span>
      </div>
    </div>

    <!-- 已接收列表 -->
    <div class="recv-section">
      <div class="recv-section-title">
        已接收 ({{ status.received_count || 0 }})
      </div>
      <div v-if="receivedItems.length === 0" class="recv-empty">
        暂无文件,等待发送…
      </div>
      <div v-for="(f, i) in receivedItems" :key="'r'+i" class="recv-row done">
        <span class="recv-icon">{{ typeIcon(f.fileType) }}</span>
        <div class="recv-info">
          <div class="recv-fname">{{ f.fileName }}</div>
          <div class="recv-meta">{{ formatSize(f.size) }} · {{ f.sender_ip }} · {{ formatTime(f.time) }}</div>
        </div>
        <el-tag type="success" size="small" effect="plain">✓ 已接收</el-tag>
      </div>
    </div>

    <template #footer>
      <span class="recv-footer-hint">关闭后将停止接收并刷新素材列表</span>
      <el-button @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed, watch, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'
import { useAssetStore } from '../stores/asset'

const props = defineProps({ modelValue: Boolean })
const emit = defineEmits(['update:modelValue'])

const assetStore = useAssetStore()
const visible = ref(props.modelValue)
const status = ref({ received: [], received_count: 0, pending: [] })
let pollTimer = null

watch(() => props.modelValue, (v) => {
  visible.value = v
  if (v) openAndStart()
})
watch(visible, (v) => emit('update:modelValue', v))

const pendingItems = computed(() => (status.value.pending || []).filter(f => !f.done))
const receivedItems = computed(() => (status.value.received || []).slice().reverse())

async function openAndStart() {
  // 启动接收端
  try {
    const { data } = await api.localsendStart()
    if (!data.running) {
      ElMessage.error(data.error || '接收端启动失败')
      visible.value = false
      return
    }
    status.value = { ...data, received: [], received_count: 0, pending: [] }
    startPoll()
  } catch (e) {
    const msg = e.response?.data?.error || e.message
    ElMessage.error('接收端启动失败: ' + msg)
    visible.value = false
  }
}

function startPoll() {
  stopPoll()
  pollTimer = setInterval(poll, 1500)
}
function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}
async function poll() {
  try {
    const { data } = await api.localsendStatus()
    status.value = data
  } catch {}
}

async function onClose() {
  stopPoll()
  const beforeCount = status.value.received_count || 0
  try {
    await api.localsendStop()
  } catch {}
  // 刷新素材框, 完成入库
  const newCount = await assetStore.scan()
  if (beforeCount > 0) {
    ElMessage.success(`本次接收 ${beforeCount} 个素材,已导入资源库`)
  } else if (newCount > 0) {
    ElMessage.success(`已导入 ${newCount} 个新素材`)
  }
}

onUnmounted(() => stopPoll())

function typeIcon(ft) {
  if (!ft) return '📄'
  if (ft.startsWith('video')) return '🎬'
  if (ft.startsWith('image')) return '🖼️'
  if (ft.startsWith('audio')) return '🎵'
  return '📄'
}
function formatSize(b) {
  if (!b) return ''
  if (b < 1048576) return (b / 1024).toFixed(0) + ' KB'
  return (b / 1048576).toFixed(1) + ' MB'
}
function formatTime(t) {
  if (!t) return ''
  const d = new Date(t * 1000)
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`
}
</script>

<style scoped>
.recv-status { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.recv-device { display: flex; align-items: center; gap: 10px; color: var(--text-primary); }
.recv-alias { font-size: 15px; font-weight: 600; }
.recv-port { font-size: 12px; color: var(--text-secondary); }
.recv-guide { margin-bottom: 14px; }
.recv-section { margin-bottom: 14px; }
.recv-section-title { font-size: 13px; color: var(--text-secondary); margin-bottom: 8px; font-weight: 600; }
.recv-row { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border: 1px solid var(--border-color); border-radius: 6px; margin-bottom: 6px; }
.recv-row.done { background: rgba(103,194,58,0.06); }
.recv-row.pending { background: rgba(230,162,60,0.06); }
.recv-icon { font-size: 22px; flex-shrink: 0; }
.recv-info { flex: 1; min-width: 0; }
.recv-fname { font-size: 13px; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.recv-meta { font-size: 11px; color: var(--text-secondary); margin-top: 2px; }
.recv-state { font-size: 12px; color: var(--warning); }
.recv-spin { color: var(--warning); }
.recv-empty { text-align: center; padding: 20px; color: var(--text-secondary); font-size: 13px; }
.recv-footer-hint { font-size: 12px; color: var(--text-secondary); margin-right: 10px; }
</style>
