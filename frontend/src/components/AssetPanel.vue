<template>
  <el-card class="asset-panel" body-style="padding:0;height:100%;display:flex;flex-direction:column;overflow:hidden;">
    <template #header>
      <div class="panel-header">
        <span>📁 资源管理</span>
        <div class="header-controls">
          <el-button size="small" type="primary" plain @click="showReceive = true">
            <el-icon><Cellphone /></el-icon> 接收
          </el-button>
          <el-tooltip :content="soundOn ? '声音: 开 (点击关闭)' : '声音: 关 (点击开启)'" placement="bottom">
            <el-button size="small" :type="soundOn ? 'primary' : 'default'" circle @click="soundOn = !soundOn">
              <el-icon><Headset v-if="soundOn" /><Mute v-else /></el-icon>
            </el-button>
          </el-tooltip>
          <el-radio-group v-model="viewMode" size="small">
            <el-radio-button value="grid"><el-icon><Grid /></el-icon></el-radio-button>
            <el-radio-button value="list"><el-icon><List /></el-icon></el-radio-button>
          </el-radio-group>
          <el-badge :value="store.assets.length" type="info" :hidden="!store.hasAssets" />
        </div>
      </div>
    </template>

    <!-- LocalSend 接收弹窗 -->
    <ReceiveDialog v-model="showReceive" />

    <!-- Tabs: 分类 -->
    <div class="tab-bar">
      <div v-for="cat in categories" :key="cat.key" class="tab-item"
        :class="{ 'tab-active': activeTab === cat.key }"
        @click="activeTab = cat.key">
        {{ cat.icon }} {{ cat.label }}
        <span class="tab-count">{{ filteredAssets(cat.key).length }}</span>
      </div>
    </div>

    <!-- Upload -->
    <div class="upload-section">
      <el-upload drag multiple :show-file-list="false" :http-request="handleUpload"
        accept=".mp4,.mov,.avi,.mkv,.jpg,.png,.jpeg,.mp3,.wav,.aac,.m4a">
        <div class="upload-inner">
          <el-icon :size="20"><UploadFilled /></el-icon>
          <span>拖拽或点击上传</span>
        </div>
      </el-upload>
    </div>

    <div v-if="store.uploading" class="upload-progress">
      <el-icon class="is-loading"><Loading /></el-icon> 上传中...
    </div>

    <!-- GRID 模式: 超大图标 -->
    <div v-if="viewMode === 'grid'" class="asset-grid scrollbar-thin">
      <div v-for="asset in currentAssets" :key="asset.name" class="grid-card"
        :class="{ 'grid-active': selectedAsset === asset.name }"
        @click="selectAsset(asset.name)">

        <!-- 视频缩略图 -->
        <div v-if="asset.type === 'video'" class="grid-video"
          @mouseenter="onVideoHover($event, asset)" @mouseleave="onVideoLeave($event, asset)">
          <video class="grid-video-el" :src="getAssetUrl(asset.path)" preload="metadata" :muted="!soundOn"
            :data-path="asset.path" @loadedmetadata="onVideoMeta(asset, $event)"></video>
          <div class="grid-play-overlay">▶</div>
          <div class="grid-duration" v-if="asset._duration">{{ asset._duration }}</div>
          <div v-if="soundOn" class="grid-sound-badge"><el-icon><Headset /></el-icon></div>
        </div>

        <!-- 图片 -->
        <div v-else-if="asset.type === 'image'" class="grid-image">
          <img :src="getAssetUrl(asset.path)" loading="lazy" />
        </div>

        <!-- 音频 -->
        <div v-else-if="asset.type === 'audio'" class="grid-icon-large">🎵</div>

        <!-- 其他 -->
        <div v-else class="grid-icon-large">📄</div>

        <!-- 底部信息 -->
        <div class="grid-info">
          <div class="grid-name" :title="asset.name">{{ asset.name }}</div>
          <div class="grid-tags">
            <el-tag v-if="asset.analysis && !asset.analysis.error" size="small" type="success">分析✓</el-tag>
            <el-tag v-if="asset._cached" size="small" type="warning">缓存</el-tag>
          </div>
        </div>

        <!-- hover 操作栏 -->
        <div class="grid-actions">
          <el-button v-if="asset.type === 'video'" size="small" type="primary" circle
            :loading="store.analyzing === asset.name" @click.stop="handleAnalyze(asset.name)">
            <el-icon><View /></el-icon>
          </el-button>
          <el-button size="small" type="danger" circle @click.stop="handleRemove(asset.name)">
            <el-icon><Delete /></el-icon>
          </el-button>
        </div>
      </div>

      <div v-if="currentAssets.length === 0" class="empty-grid">
        <el-icon :size="48" color="#c0c4cc"><FolderOpened /></el-icon>
        <p>{{ activeTab === 'all' ? '暂无资源' : `暂无${getCatLabel(activeTab)}` }}</p>
      </div>
    </div>

    <!-- LIST 模式 -->
    <div v-else class="asset-list scrollbar-thin">
      <div v-for="asset in currentAssets" :key="asset.name" class="list-row"
        :class="{ 'list-active': selectedAsset === asset.name }"
        @click="selectAsset(asset.name)">
        <div class="list-icon">
          <template v-if="asset.type === 'video'">
            <div class="list-video-mini" @mouseenter="onVideoHover($event, asset)" @mouseleave="onVideoLeave($event, asset)">
              <video class="list-video-el" :src="getAssetUrl(asset.path)" preload="metadata" :muted="!soundOn" :data-path="asset.path"></video>
            </div>
          </template>
          <template v-else>{{ typeIcon(asset.type) }}</template>
        </div>
        <div class="list-info">
          <div class="list-name">{{ asset.name }}</div>
          <div class="list-tags">
            <el-tag size="small" effect="plain">{{ asset.type }}</el-tag>
            <el-tag v-if="asset.analysis && !asset.analysis.error" size="small" type="success">✓</el-tag>
          </div>
        </div>
        <div class="list-actions" @click.stop>
          <el-button v-if="asset.type === 'video'" size="small" :loading="store.analyzing === asset.name" @click="handleAnalyze(asset.name)">分析</el-button>
          <el-button size="small" type="danger" plain circle @click="handleRemove(asset.name)"><el-icon><Delete /></el-icon></el-button>
        </div>
      </div>
      <div v-if="currentAssets.length === 0" class="empty-grid"><p>暂无资源</p></div>
    </div>

    <!-- 分析结果 -->
    <div v-if="currentAnalysis" class="analysis-result scrollbar-thin">
      <el-divider content-position="left"><el-icon><DataAnalysis /></el-icon> 分析结果</el-divider>
      <div v-if="currentAnalysis.error" class="analysis-error">❌ {{ currentAnalysis.error }}</div>
      <div v-else class="analysis-content">
        <div class="analysis-meta">
          <el-tag size="small">📐 {{ a.meta?.width || '?' }}×{{ a.meta?.height || '?' }}</el-tag>
          <el-tag size="small" type="info">{{ (a.meta?.duration || 0).toFixed(1) }}秒</el-tag>
          <el-tag v-if="a.scenes" size="small" type="warning">{{ a.scenes.length }}场景</el-tag>
        </div>
        <div v-if="a.content" class="ai-row"><span class="ai-emoji">🎬</span><span>{{ a.content }}</span></div>
        <div v-if="a.mood" class="ai-row"><span class="ai-emoji">🎭</span><span>{{ a.mood }}</span></div>
        <div v-if="a.quality" class="ai-row"><span class="ai-emoji">⭐</span><span>{{ a.quality }}</span></div>
        <div v-if="a.highlights?.length" class="ai-row"><span class="ai-emoji">🔥</span><div class="tag-list"><el-tag v-for="(h,i) in a.highlights" :key="i" size="small" effect="plain">{{ h }}</el-tag></div></div>
        <div v-if="a.suitable_for?.length" class="ai-row"><span class="ai-emoji">💡</span><div class="tag-list"><el-tag v-for="(s,i) in a.suitable_for" :key="i" size="small" type="info" effect="plain">{{ s }}</el-tag></div></div>
      </div>
    </div>
  </el-card>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { useAssetStore } from '../stores/asset'
import { ElMessageBox } from 'element-plus'
import api from '../api'
import ReceiveDialog from './ReceiveDialog.vue'

const store = useAssetStore()
const selectedAsset = ref(null)
const viewMode = ref('grid')
const activeTab = ref('all')
const soundOn = ref(false)   // 声音开关: false=静音预览, true=带声预览
const showReceive = ref(false)   // LocalSend 接收弹窗

// 声音开关变化时, 同步当前正在播放的视频静音状态
watch(soundOn, (on) => {
  document.querySelectorAll('video.grid-video-el, video.list-video-el').forEach(v => { v.muted = !on })
})

// 挂载时: 扫描目录加载已有素材
onMounted(async () => {
  await store.scan()
})

const categories = [
  { key: 'all', label: '全部', icon: '📂' },
  { key: 'video', label: '视频', icon: '🎬' },
  { key: 'audio', label: '音频', icon: '🎵' },
  { key: 'other', label: '其他', icon: '📄' },
]

function filteredAssets(cat) {
  if (cat === 'all') return store.assets
  if (cat === 'other') return store.assets.filter(a => a.type === 'image' || a.type === 'other')
  return store.assets.filter(a => a.type === cat)
}
const currentAssets = computed(() => filteredAssets(activeTab.value))

function getCatLabel(k) { return categories.find(c => c.key === k)?.label || k }

const currentAnalysis = computed(() => {
  if (!selectedAsset.value) return null
  return store.analysisResult[selectedAsset.value] || store.assets.find(a => a.name === selectedAsset.value)?.analysis || null
})
const a = computed(() => {
  if (!currentAnalysis.value) return {}
  try { return { ...currentAnalysis.value, ...JSON.parse((currentAnalysis.value.visual_analysis || '').match(/\{[\s\S]*\}/)?.[0] || '{}') } }
  catch { return { ...currentAnalysis.value } }
})
const analysisData = a

// === 资源 URL ===
function getAssetUrl(path) { return `/api/video/serve?path=${encodeURIComponent(path)}` }

// === 视频 hover 预览 ===
const playbackMemory = {}
function onVideoHover(e, asset) {
  const v = e.currentTarget.querySelector('video'); if (!v) return
  const saved = playbackMemory[asset.path]; if (saved) v.currentTime = saved
  v.play().catch(() => {})
}
function onVideoLeave(e, asset) {
  const v = e.currentTarget.querySelector('video'); if (!v) return
  playbackMemory[asset.path] = v.currentTime; v.pause()
}
function onVideoMeta(asset, e) {
  const d = e.target.duration; if (!d || !isFinite(d)) return
  const m = Math.floor(d / 60), s = Math.floor(d % 60)
  asset._duration = m > 0 ? `${m}:${String(s).padStart(2,'0')}` : `${Math.round(d)}s`
}

function typeIcon(t) { return { video: '🎬', audio: '🎵', image: '🖼️', other: '📄' }[t] || '📄' }
function selectAsset(name) { selectedAsset.value = name }
async function handleUpload({ file }) { await store.upload([file]) }
async function handleAnalyze(name) { selectedAsset.value = name; await store.analyze(name) }
async function handleRemove(name) {
  try { await ElMessageBox.confirm(`删除 ${name}?`, '确认', { type: 'warning' }); store.remove(name); if (selectedAsset.value === name) selectedAsset.value = null } catch {}
}
</script>

<style scoped>
.asset-panel { background: var(--bg-card); border: 1px solid var(--border-color); }
.panel-header { display: flex; align-items: center; justify-content: space-between; font-weight: 600; }
.header-controls { display: flex; align-items: center; gap: 8px; }

/* Tabs */
.tab-bar { display: flex; gap: 0; border-bottom: 1px solid var(--border-color); background: var(--bg-input); }
.tab-item {
  padding: 6px 14px; font-size: 13px; cursor: pointer; color: var(--text-secondary);
  border-bottom: 2px solid transparent; transition: all 0.2s; user-select: none; white-space: nowrap;
}
.tab-item:hover { color: var(--accent); }
.tab-active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
.tab-count { font-size: 10px; color: #c0c4cc; margin-left: 2px; }

/* Upload */
.upload-section { padding: 8px; }
.upload-section :deep(.el-upload) { width: 100%; }
.upload-section :deep(.el-upload-dragger) {
  background: var(--bg-input); border: 1px dashed var(--border-color); border-radius: 6px;
  padding: 8px; transition: border-color 0.3s; width: 100%; height: auto;
}
.upload-section :deep(.el-upload-dragger:hover) { border-color: var(--accent); }
.upload-inner { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-secondary); }
.upload-progress { text-align: center; padding: 2px; color: var(--accent); font-size: 11px; }

/* ===== GRID 模式 (超大图标) ===== */
.asset-grid {
  flex: 1; overflow-y: auto; padding: 10px;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px; align-content: start;
}
.grid-card {
  border: 1px solid var(--border-color); border-radius: 10px; overflow: hidden;
  background: var(--bg-card); cursor: pointer; transition: all 0.25s; position: relative;
  display: flex; flex-direction: column;
}
.grid-card:hover {
  border-color: var(--accent); box-shadow: 0 4px 16px rgba(64,158,255,0.15); transform: translateY(-2px);
}
.grid-active { border-color: var(--accent) !important; box-shadow: 0 0 0 2px rgba(64,158,255,0.3); }

/* 视频缩略图区域 */
.grid-video { width: 100%; aspect-ratio: 16/9; background: #000; position: relative; overflow: hidden; }
.grid-video-el { width: 100%; height: 100%; object-fit: cover; pointer-events: none; display: block; }
.grid-play-overlay {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
  font-size: 40px; color: rgba(255,255,255,0.7); opacity: 0; transition: opacity 0.2s;
  text-shadow: 0 2px 8px rgba(0,0,0,0.5);
}
.grid-card:hover .grid-play-overlay { opacity: 0; }  /* hover 时直接播放, 不需要 overlay */
.grid-duration {
  position: absolute; bottom: 6px; right: 6px; font-size: 11px; color: #fff;
  background: rgba(0,0,0,0.75); padding: 2px 6px; border-radius: 4px;
}
.grid-sound-badge {
  position: absolute; bottom: 6px; left: 6px; font-size: 12px; color: var(--accent);
  background: rgba(0,0,0,0.7); padding: 2px 6px; border-radius: 4px;
  display: flex; align-items: center; gap: 3px;
}

/* 图片 */
.grid-image { width: 100%; aspect-ratio: 16/9; overflow: hidden; background: var(--bg-input); }
.grid-image img { width: 100%; height: 100%; object-fit: cover; }

/* 音频/其他大图标 */
.grid-icon-large {
  width: 100%; aspect-ratio: 16/9; display: flex; align-items: center; justify-content: center;
  font-size: 64px; background: var(--bg-input);
}

/* 底部信息 */
.grid-info { padding: 8px 10px; flex: 1; }
.grid-name { font-size: 12px; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 4px; }
.grid-tags { display: flex; gap: 3px; flex-wrap: wrap; }

/* hover 操作栏 */
.grid-actions {
  position: absolute; top: 6px; right: 6px; display: flex; gap: 4px;
  opacity: 0; transition: opacity 0.2s;
}
.grid-card:hover .grid-actions { opacity: 1; }

/* 空状态 */
.empty-grid {
  grid-column: 1 / -1; text-align: center; padding: 40px 0; color: var(--text-secondary);
}
.empty-grid p { margin-top: 8px; font-size: 13px; }

/* ===== LIST 模式 ===== */
.asset-list { flex: 1; overflow-y: auto; padding: 6px; }
.list-row {
  display: flex; align-items: center; gap: 10px; padding: 8px;
  border: 1px solid var(--border-color); border-radius: 6px; margin-bottom: 5px;
  background: var(--bg-card); cursor: pointer; transition: all 0.15s;
}
.list-row:hover { border-color: var(--accent); }
.list-active { border-color: var(--accent) !important; background: rgba(64,158,255,0.05) !important; }
.list-icon { font-size: 24px; flex-shrink: 0; width: 40px; text-align: center; }
.list-video-mini { width: 48px; height: 27px; border-radius: 3px; overflow: hidden; background: #000; }
.list-video-el { width: 100%; height: 100%; object-fit: cover; pointer-events: none; }
.list-info { flex: 1; min-width: 0; }
.list-name { font-size: 12px; color: var(--text-primary); word-break: break-all; }
.list-tags { margin-top: 3px; display: flex; gap: 3px; }
.list-actions { display: flex; gap: 4px; flex-shrink: 0; }

/* 分析结果 */
.analysis-result { max-height: 220px; overflow-y: auto; padding: 0 12px 8px; border-top: 1px solid var(--border-color); }
.analysis-error { color: var(--danger); font-size: 13px; padding: 8px; }
.analysis-content { font-size: 12px; line-height: 1.6; }
.analysis-meta { display: flex; gap: 4px; margin: 8px 0; flex-wrap: wrap; }
.ai-row { margin-bottom: 6px; display: flex; gap: 6px; align-items: flex-start; }
.ai-emoji { flex-shrink: 0; }
.tag-list { display: flex; flex-wrap: wrap; gap: 4px; }
</style>
