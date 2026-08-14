import { defineStore } from 'pinia'
import api from '../api'
import { ElMessage, ElNotification } from 'element-plus'

export const useAssetStore = defineStore('asset', {
  state: () => ({
    assets: [],
    uploading: false,
    analyzing: null,
    analysisResult: {},
  }),
  getters: {
    hasAssets: (s) => s.assets.length > 0,
    videoAssets: (s) => s.assets.filter(a => a.type === 'video'),
  },
  actions: {
    async upload(rawFiles) {
      this.uploading = true
      try {
        const formData = new FormData()
        for (const f of rawFiles) formData.append('files', f)
        const { data } = await api.upload(formData)
        if (data.assets) {
          // 上传后自动检查每个资源是否已有缓存分析
          for (const asset of data.assets) {
            await this.checkCache(asset)
          }
          this.assets.push(...data.assets)
          ElMessage.success(`已导入 ${data.assets.length} 个资源`)
        }
        return data
      } catch (e) {
        ElMessage.error('上传失败: ' + (e.response?.data?.error || e.message))
        return null
      } finally {
        this.uploading = false
      }
    },

    async checkCache(asset) {
      if (asset.type !== 'video') return
      try {
        const { data } = await api.checkCached(asset.path)
        if (data.cached && data.result) {
          asset.analysis = data.result
          asset._cached = true
          this.analysisResult[asset.name] = data.result
        }
      } catch {}
    },

    /**扫描 render_uploads/ 目录刷新素材列表 (LocalSend 收到新文件后用)
     * 合并新出现的资源 (保留已分析/已加载的), 返回新增数量 */
    async scan() {
      try {
        const { data } = await api.scanAssets()
        const existing = new Map(this.assets.map(a => [a.path, a]))
        let newCount = 0
        for (const a of (data.assets || [])) {
          if (!existing.has(a.path)) {
            // 新资源, 检查是否已有缓存分析
            await this.checkCache(a)
            this.assets.push(a)
            newCount++
          }
        }
        // 移除已不存在的 (用户手动删了文件)
        const paths = new Set((data.assets || []).map(a => a.path))
        this.assets = this.assets.filter(a => paths.has(a.path))
        return newCount
      } catch (e) {
        return 0
      }
    },

    /**轮询扫描: LocalSend 接收端活跃时, 每 2 秒扫一次目录, 发现新文件即提示 */
    startLocalSendWatch() {
      if (this._lsWatch) return
      this._lsWatch = setInterval(async () => {
        try {
          const { data } = await api.localsendStatus()
          if (!data.running) { this.stopLocalSendWatch(); return }
          const n = await this.scan()
          if (n > 0) {
            ElMessage.success(`LocalSend 收到 ${n} 个新素材`)
          }
        } catch {}
      }, 2000)
    },

    stopLocalSendWatch() {
      if (this._lsWatch) { clearInterval(this._lsWatch); this._lsWatch = null }
    },

    async analyze(assetName) {
      const asset = this.assets.find(a => a.name === assetName)
      if (!asset) {
        ElMessage.warning('未找到资源')
        return null
      }
      if (asset.type !== 'video') {
        ElMessage.warning('只能分析视频文件')
        return null
      }

      // 已有缓存 → 直接加载
      if (asset.analysis && !asset.analysis.error) {
        this.analysisResult[assetName] = asset.analysis
        ElMessage.info('已加载缓存的分析结果')
        return asset.analysis
      }

      this.analyzing = assetName
      ElMessage.info(`正在分析 ${assetName}...（约 20-30 秒，VLM + ASR）`)

      try {
        const { data } = await api.analyzeByPath(asset.path)
        asset.analysis = data
        this.analysisResult[assetName] = data

        const isCached = data._cached
        const summary = this._formatAnalysis(data)
        ElNotification({
          title: isCached ? `从缓存加载: ${assetName}` : `分析完成: ${assetName}`,
          message: summary,
          type: 'success',
          duration: 8000,
          position: 'bottom-right',
        })
        return data
      } catch (e) {
        const errMsg = e.response?.data?.error || e.message || '未知错误'
        ElMessage.error(`分析失败: ${errMsg}`)
        asset.analysis = { error: errMsg }
        return null
      } finally {
        this.analyzing = null
      }
    },

    async reanalyze(assetName) {
      const asset = this.assets.find(a => a.name === assetName)
      if (!asset) return
      asset.analysis = null
      delete this.analysisResult[assetName]
      // force=true 跳过缓存
      this.analyzing = assetName
      ElMessage.info(`重新分析 ${assetName}...`)
      try {
        const { data } = await api.post('/api/perceive', { path: asset.path, force: true }, { timeout: 120000 })
        asset.analysis = data
        this.analysisResult[assetName] = data
        ElNotification({ title: `重新分析完成`, message: this._formatAnalysis(data), type: 'success', duration: 8000 })
        return data
      } catch (e) {
        ElMessage.error(`分析失败: ${e.message}`)
      } finally {
        this.analyzing = null
      }
    },

    _formatAnalysis(result) {
      if (!result) return '无结果'
      const visual = result.visual_analysis || ''
      try {
        const json = JSON.parse(visual.match(/\{[\s\S]*\}/)?.[0] || '{}')
        return [
          json.content || '',
          json.mood ? '情绪: ' + json.mood : '',
          json.quality ? '质量: ' + json.quality : '',
        ].filter(Boolean).join('\n')
      } catch {
        return visual.slice(0, 200)
      }
    },

    remove(name) {
      this.assets = this.assets.filter(a => a.name !== name)
      delete this.analysisResult[name]
    }
  }
})
