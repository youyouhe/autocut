import { useState, useRef, useEffect, useCallback } from 'react';
import { Upload, Play, Info, FileVideo, Image as ImageIcon, Music, RefreshCw, Headset, VolumeOff, VolumeX, Loader2, Trash2, Captions, FileText, Scissors, Star, Plus, Search, CheckSquare, SquareCheck, Check } from 'lucide-react';
import * as api from '../api';
import type { Asset, AssetType, Shot, MainVideo } from '../api';
import AssetDetailPopup from './AssetDetailPopup';

/**
 * 视口内懒加载 hook: 元素进入视口前不渲染媒体 src, 省掉视口外卡片的字节请求.
 * rootMargin 200px 提前量 —— 快滚到时就开始加载, 避免滚到位才出图.
 * 返回 [ref, inView]; inView 一旦为 true 永久置 true (缩略图该出就出, 不再收回).
 */
function useInView<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [inView, setInView] = useState(false);
  useEffect(() => {
    if (inView) return;
    const el = ref.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      (entries) => { if (entries[0]?.isIntersecting) setInView(true); },
      { rootMargin: '200px' },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [inView]);
  return [ref, inView] as const;
}

interface Props {
  assets: Asset[];
  setAssets: React.Dispatch<React.SetStateAction<Asset[]>>;
  refreshAssets: () => Promise<void>;
  draftId: string | null;
}

const playbackMemory: Record<string, number> = {};

/** 字节数 → MB 文本 (卡片信息行用). */
const formatSize = (bytes?: number) => bytes ? (bytes / 1048576).toFixed(2) + ' MB' : '';

/** 非视频/图片/音频素材的占位图标 (subtitle/text/other). */
const getIcon = (type: string, size = 32) => {
  if (type === 'video') return <FileVideo size={size} strokeWidth={1} className="text-white/30" />;
  if (type === 'image') return <ImageIcon size={size} strokeWidth={1} className="text-white/30" />;
  if (type === 'audio') return <Music size={size} strokeWidth={1} className="text-white/30" />;
  if (type === 'subtitle') return <Captions size={size} strokeWidth={1} className="text-white/30" />;
  if (type === 'text') return <FileText size={size} strokeWidth={1} className="text-white/30" />;
  return <FileVideo size={size} strokeWidth={1} className="text-white/30" />;
};

export default function AssetPanel({ assets, setAssets, refreshAssets, draftId }: Props) {
  const [uploading, setUploading] = useState(false);
  const [analyzing, setAnalyzing] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [strippingAudio, setStrippingAudio] = useState<string | null>(null);
  const [splitting, setSplitting] = useState<string | null>(null);
  const [shotsMap, setShotsMap] = useState<Record<string, Shot[] | null>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [popupAnchor, setPopupAnchor] = useState<DOMRect | null>(null);
  const [soundOn, setSoundOn] = useState(false);
  const [toast, setToast] = useState<{ msg: string; type: 'success' | 'error' } | null>(null);
  const [mainVideo, setMainVideoState] = useState<MainVideo | null>(null);
  const [mainVideoPortrait, setMainVideoPortrait] = useState(false);
  const [settingMain, setSettingMain] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [selectMode, setSelectMode] = useState(false);
  const [checked, setChecked] = useState<Set<string>>(new Set());  // asset.path 集合
  const [bulkDeleting, setBulkDeleting] = useState(false);
  const [typeFilter, setTypeFilter] = useState<'all' | AssetType>('all');
  const [adding, setAdding] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.getMainVideo().then(r => setMainVideoState(r.main_video)).catch(() => {});
  }, []);

  const showToast = (msg: string, type: 'success' | 'error' = 'success') => {
    setToast({ msg, type });
    setTimeout(() => setToast(t => (t?.msg === msg ? null : t)), 3000);
  };

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!e.target.files?.length) return;
    setUploading(true);
    try {
      await api.upload(e.target.files);
      await refreshAssets();
    } catch (err) { console.error(err); }
    finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const ANALYZABLE: AssetType[] = ['video', 'image'];

  /** 打开某卡片的就地 popup: 记下被点卡片的屏幕矩形作为锚点, popup 会浮在它旁边.
   * 用 setTimeout 确保 DOMRect 在点击瞬间捕获 (而非渲染后再取). */
  const openPopup = useCallback((asset: Asset, e?: React.MouseEvent) => {
    const rect = (e?.currentTarget as HTMLElement)?.getBoundingClientRect() ?? null;
    setPopupAnchor(rect);
    setSelected(asset.name);
  }, []);

  const closePopup = useCallback(() => {
    setSelected(null);
    setPopupAnchor(null);
  }, []);

  const handleAnalyze = async (asset: Asset, force = false, e?: React.MouseEvent) => {
    if (!ANALYZABLE.includes(asset.type)) return;
    openPopup(asset, e);
    if (!force && asset.analysis && !('error' in (asset.analysis as any))) return; // 已有, 仅展示
    setAnalyzing(asset.name);
    try {
      const result = await api.perceive(asset.path, { force });
      setAssets(prev => prev.map(a => a.path === asset.path ? { ...a, analysis: result, _cached: result._cached } : a));
    } catch (err: any) {
      setAssets(prev => prev.map(a => a.path === asset.path ? { ...a, analysis: { error: err.message } } : a));
    } finally { setAnalyzing(null); }
  };

  // 选中一个视频时, 先看看之前有没有拆过分镜(纯读缓存, 不触发拆分), 有就直接展示
  useEffect(() => {
    if (!selected || shotsMap[selected] !== undefined) return;
    const asset = assets.find(a => a.name === selected);
    if (!asset || asset.type !== 'video') return;
    api.getShots(asset.name).then(r => setShotsMap(prev => ({ ...prev, [asset.name]: r.shots }))).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  const handleSplitShots = async (asset: Asset, force = false, e?: React.MouseEvent) => {
    openPopup(asset, e);
    setSplitting(asset.name);
    try {
      const r = await api.splitShots(asset.name, { force });
      if (r.ok && r.shots) {
        setShotsMap(prev => ({ ...prev, [asset.name]: r.shots! }));
        // 切出来的镜头视频已作为素材落到资产目录, 刷新列表让它自动出现在资产里
        await refreshAssets();
        showToast(`拆出 ${r.shots.length} 个镜头`, 'success');
      } else if (r.ok && (r as any).started) {
        // 异步拆分: 大视频 CPU 推理要 5-15 分钟, 立即返回; 完成后自动刷新
        showToast(`已开始后台拆分 (约 5-15 分钟), 完成后镜头自动出现在素材列表`, 'success');
        const name = asset.name;
        const poll = setInterval(async () => {
          try {
            const s = await api.getShots(name);
            if (s.shots) {
              clearInterval(poll);
              setShotsMap(prev => ({ ...prev, [name]: s.shots! }));
              await refreshAssets();
              showToast(`拆出 ${s.shots.length} 个镜头`, 'success');
            } else if (s.split_error) {
              clearInterval(poll);
              showToast(`分镜拆分失败: ${s.split_error}`, 'error');
            }
          } catch { /* 轮询失败继续等 */ }
        }, 20000);
        setTimeout(() => clearInterval(poll), 30 * 60 * 1000); // 最多轮询30分钟
      } else {
        showToast(`分镜拆分失败: ${r.error || '未知错误'}`, 'error');
      }
    } catch (err: any) {
      showToast(`分镜拆分失败: ${err.message}`, 'error');
    } finally { setSplitting(null); }
  };

  const handleSetMain = async (asset: Asset) => {
    setSettingMain(asset.name);
    try {
      const r = await api.setMainVideo(asset.name);
      if (r.ok && r.main_video) {
        setMainVideoState(r.main_video);
        showToast(`已将 "${asset.name}" 设为主视频`, 'success');
      } else {
        showToast(`设为主视频失败: ${r.error || '未知错误'}`, 'error');
      }
    } catch (err: any) {
      showToast(`设为主视频失败: ${err.message}`, 'error');
    } finally { setSettingMain(null); }
  };

  const handleClearMain = async () => {
    try {
      await api.clearMainVideo();
      setMainVideoState(null);
    } catch (err: any) {
      showToast(`操作失败: ${err.message}`, 'error');
    }
  };

  const onVideoHover = (e: React.MouseEvent, path: string) => {
    const v = (e.currentTarget as HTMLElement).querySelector('video'); if (!v) return;
    const saved = playbackMemory[path]; if (saved) v.currentTime = saved;
    v.play().catch(() => {});
  };
  const onVideoLeave = (e: React.MouseEvent, path: string) => {
    const v = (e.currentTarget as HTMLElement).querySelector('video'); if (!v) return;
    playbackMemory[path] = v.currentTime; v.pause();
  };
  const onVideoMeta = (asset: Asset, e: React.SyntheticEvent<HTMLVideoElement>) => {
    const v = e.currentTarget;
    const d = v.duration; if (!d || !isFinite(d)) return;
    const m = Math.floor(d / 60), s = Math.floor(d % 60);
    const dur = m > 0 ? `${m}:${String(s).padStart(2, '0')}` : `${Math.round(d)}s`;
    const portrait = v.videoHeight > v.videoWidth;
    setAssets(prev => prev.map(a => a.path === asset.path ? { ...a, _duration: dur, _portrait: portrait } : a));
  };

  const handleDelete = async (asset: Asset) => {
    if (!confirm(`删除 "${asset.name}"? 不可恢复.`)) return;
    setDeleting(asset.name);
    try {
      await api.deleteAsset(asset.name);
      setAssets(prev => prev.filter(a => a.path !== asset.path));
      if (selected === asset.name) closePopup();
    } catch (err: any) {
      alert('删除失败: ' + err.message);
    } finally { setDeleting(null); }
  };

  // ===== 多选批量操作 =====
  const toggleChecked = (asset: Asset) => {
    setChecked(prev => {
      const n = new Set(prev);
      if (n.has(asset.path)) n.delete(asset.path); else n.add(asset.path);
      return n;
    });
  };

  const checkAllFiltered = () => {
    // 全选 = 选中当前搜索/类型筛选后的可见集合 (再点一次全不选)
    const allChecked = filtered.length > 0 && filtered.every(a => checked.has(a.path));
    if (allChecked) {
      setChecked(prev => { const n = new Set(prev); filtered.forEach(a => n.delete(a.path)); return n; });
    } else {
      setChecked(prev => { const n = new Set(prev); filtered.forEach(a => n.add(a.path)); return n; });
    }
  };

  const handleBulkDelete = async () => {
    const targets = assets.filter(a => checked.has(a.path));
    if (targets.length === 0) return;
    const totalMB = (targets.reduce((s, a) => s + (a.size || 0), 0) / 1048576).toFixed(0);
    if (!confirm(`删除选中的 ${targets.length} 个素材 (共 ${totalMB}MB)? 不可恢复.`)) return;
    setBulkDeleting(true);
    let ok = 0, fail = 0;
    for (const a of targets) {
      try {
        await api.deleteAsset(a.name);
        setAssets(prev => prev.filter(x => x.path !== a.path));
        if (selected === a.name) closePopup();
        ok++;
      } catch { fail++; }
    }
    setBulkDeleting(false);
    setChecked(new Set());
    setSelectMode(false);
    showToast(`批量删除完成: 成功 ${ok}${fail ? `, 失败 ${fail}` : ''}`, fail ? 'error' : 'success');
  };

  const handleStripAudio = async (asset: Asset) => {
    if (!confirm(`去除 "${asset.name}" 的音轨? 不可恢复。去除后该素材没有声音，分析时只用画面(VLM)匹配，不再走 ASR。`)) return;
    setStrippingAudio(asset.name);
    try {
      await api.stripAudio(asset.name);
      // 音轨变了, 旧的分析结果(可能含之前的转录)已经失效, 清掉让用户重新分析
      setAssets(prev => prev.map(a => a.path === asset.path ? { ...a, has_audio: false, analysis: undefined, _cached: undefined } : a));
      showToast(`已去除 "${asset.name}" 的音轨`, 'success');
    } catch (err: any) {
      showToast(`去除声音失败: ${err.message}`, 'error');
    } finally { setStrippingAudio(null); }
  };

  const handleAddToDraft = async (asset: Asset) => {
    if (!draftId) { showToast('无激活草稿, 请先在 Drafts 新建或打开草稿', 'error'); return; }
    setAdding(asset.name);
    try {
      const r = await api.addAssetToDraft(draftId, asset.path, asset.type as 'video' | 'audio' | 'image');
      if (r.ok && r.duplicate) {
        showToast(r.note || '该素材已在草稿中, 已跳过', 'error');
      } else if (r.ok) {
        showToast(`已添加到草稿 ${draftId.slice(0, 12)}…`, 'success');
      } else {
        showToast('添加失败: ' + (r.error || '未知错误'), 'error');
      }
    } catch (err) {
      showToast('添加失败: ' + (err as Error).message, 'error');
    } finally { setAdding(null); }
  };

  const selectedAsset = assets.find(a => a.name === selected) || null;

  // 搜索 + 类型筛选 (即时过滤, 素材库规模小无需 debounce)
  const filtered = assets.filter(a => {
    if (typeFilter !== 'all' && a.type !== typeFilter) return false;
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return a.name.toLowerCase().includes(q)
      || (a.analysis && 'tags' in (a.analysis as any) && Array.isArray((a.analysis as any).tags)
            ? ((a.analysis as any).tags as string[]).some(t => t.toLowerCase().includes(q)) : false);
  });

  return (
    <div className="h-full w-full flex flex-col p-12 overflow-y-auto relative">
      {toast && (
        <div className={`fixed top-6 right-6 z-50 px-4 py-3 text-sm shadow-lg ${toast.type === 'success' ? 'bg-emerald-700 text-white' : 'bg-red-700 text-white'}`}>
          {toast.msg}
        </div>
      )}
      <div className="flex items-center justify-between mb-6 border-b border-[#121212]/10 pb-6">
        <h2 className="text-4xl font-light italic font-serif">Digital Assets</h2>
        <div className="flex gap-4 items-center">
          <button
            onClick={() => setSoundOn(s => !s)}
            className="flex items-center gap-2 px-4 py-2 border border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5 transition-colors text-[10px] uppercase tracking-widest font-bold"
            title={soundOn ? '声音开' : '声音关'}
          >
            {soundOn ? <Headset size={14} strokeWidth={1.5} /> : <VolumeOff size={14} strokeWidth={1.5} />}
          </button>
          <button
            onClick={refreshAssets}
            className="flex items-center gap-2 px-4 py-2 border border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5 transition-colors text-[10px] uppercase tracking-widest font-bold"
          >
            <RefreshCw size={14} strokeWidth={1.5} /> Refresh
          </button>
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            className="flex items-center gap-2 px-4 py-2 bg-[#121212] hover:bg-[#121212]/80 text-white transition-colors disabled:opacity-50 text-[10px] uppercase tracking-widest font-bold"
          >
            {uploading ? <Loader2 size={14} strokeWidth={1.5} className="animate-spin" /> : <Upload size={14} strokeWidth={1.5} />}
            {uploading ? 'Uploading...' : 'Upload'}
          </button>
          <input type="file" multiple className="hidden" ref={fileInputRef} onChange={handleFileChange}
            accept=".mp4,.mov,.avi,.mkv,.jpg,.png,.jpeg,.webp,.mp3,.wav,.aac,.m4a,.srt,.txt" />
        </div>
      </div>

      {/* 搜索 / 类型筛选 */}
      <div className="flex items-center gap-3 mb-8">
        <div className="flex-1 relative">
          <Search size={14} strokeWidth={1.5} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#121212]/40" />
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search assets…"
            className="w-full border border-[#121212]/20 focus:border-[#121212] outline-none pl-9 pr-4 py-2 bg-white text-[#121212] font-light transition-colors"
          />
        </div>
        <select
          value={typeFilter}
          onChange={e => setTypeFilter(e.target.value as 'all' | AssetType)}
          className="border border-[#121212]/20 focus:border-[#121212] outline-none px-3 py-2 bg-white text-[#121212] text-[10px] uppercase tracking-widest font-bold transition-colors cursor-pointer"
        >

          <option value="all">All</option>
          <option value="video">Video</option>
          <option value="image">Image</option>
          <option value="audio">Audio</option>
          <option value="subtitle">Subtitle</option>
          <option value="text">Text</option>
        </select>
        {/* 多选模式切换 */}
        <button
          onClick={() => { setSelectMode(m => !m); setChecked(new Set()); }}
          className={`flex items-center gap-1.5 px-3 py-2 border text-[10px] uppercase tracking-widest font-bold transition-colors ${selectMode ? 'border-[#121212] bg-[#121212] text-[#FDFCF8]' : 'border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5'}`}
          title="批量选择素材 (全选/批量删除)">
          <CheckSquare size={13} strokeWidth={2} /> {selectMode ? '退出选择' : '选择'}
        </button>
        {selectMode && (
          <>
            <button
              onClick={checkAllFiltered}
              className="flex items-center gap-1.5 px-3 py-2 border border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5 transition-colors text-[10px] uppercase tracking-widest font-bold"
              title="全选/取消全选当前筛选结果">
              <SquareCheck size={13} strokeWidth={2} /> 全选
            </button>
            <button
              onClick={handleBulkDelete}
              disabled={checked.size === 0 || bulkDeleting}
              className="flex items-center gap-1.5 px-3 py-2 border border-red-700/40 text-red-700 hover:bg-red-50 transition-colors disabled:opacity-40 text-[10px] uppercase tracking-widest font-bold">
              {bulkDeleting ? <Loader2 size={13} strokeWidth={2} className="animate-spin" /> : <Trash2 size={13} strokeWidth={2} />}
              删除{checked.size > 0 ? ` (${checked.size})` : ''}
            </button>
          </>
        )}
      </div>

      {/* 主视频 (最新录制的那条, 跟长期存在的素材库分开管理) */}
      <div className="mb-10 border border-amber-600/30 bg-amber-50/40 p-6">
        <div className="flex items-center justify-between mb-3">
          <div className="text-[10px] uppercase tracking-widest font-bold text-amber-800 flex items-center gap-1.5">
            <Star size={12} strokeWidth={2} fill="currentColor" /> 主视频
          </div>
          {mainVideo && (
            <button onClick={handleClearMain} className="text-[10px] uppercase tracking-widest opacity-50 hover:opacity-100 transition-opacity">取消标记</button>
          )}
        </div>
        {mainVideo ? (
          <div className="flex items-start gap-6">
            <div className={`${mainVideoPortrait ? 'w-40 h-64' : 'w-96 h-56'} bg-[#121212] flex-shrink-0 overflow-hidden`}
              onMouseEnter={(e) => onVideoHover(e, mainVideo.path)}
              onMouseLeave={(e) => onVideoLeave(e, mainVideo.path)}>
              {mainVideo.url && (
                <video
                  className={`w-full h-full ${mainVideoPortrait ? 'object-contain' : 'object-cover'}`}
                  src={mainVideo.url}
                  poster={mainVideo.poster_url}
                  controls
                  muted={!soundOn}
                  preload="metadata"
                  playsInline
                  onLoadedMetadata={(e) => setMainVideoPortrait(e.currentTarget.videoHeight > e.currentTarget.videoWidth)}
                />
              )}
            </div>
            <div className="pt-1">
              <div className="text-lg font-serif italic truncate">{mainVideo.name}</div>
              <div className="text-[10px] uppercase tracking-widest opacity-50 mt-1">
                标记于 {new Date(mainVideo.set_at * 1000).toLocaleString()}
              </div>
            </div>
          </div>
        ) : (
          <p className="text-sm opacity-50 font-light">还没有标记主视频 —— 在下面素材库里选一个视频，点"设为主视频"</p>
        )}
      </div>

      {assets.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center text-[#121212]/50">
          <FileVideo size={48} className="mb-4 opacity-20" strokeWidth={1} />
          <p className="font-serif italic text-2xl">No assets found</p>
          <p className="text-[10px] uppercase tracking-widest mt-2">Upload files or use LocalSend</p>
        </div>
      ) : (
        <>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-8">
          {filtered.map((asset) => (
            <AssetCard
              key={asset.path}
              asset={asset}
              selected={selected === asset.name}
              selectMode={selectMode}
              checked={checked.has(asset.path)}
              onToggleCheck={() => toggleChecked(asset)}
              soundOn={soundOn}
              mainVideoName={mainVideo?.name}
              analyzing={analyzing === asset.name}
              splitting={splitting === asset.name}
              deleting={deleting === asset.name}
              strippingAudio={strippingAudio === asset.name}
              settingMain={settingMain === asset.name}
              adding={adding === asset.name}
              hasShots={!!shotsMap[asset.name]}
              draftId={draftId}
              onHover={onVideoHover}
              onLeave={onVideoLeave}
              onMeta={onVideoMeta}
              onAnalyze={(e) => handleAnalyze(asset, false, e)}
              onReanalyze={(e) => handleAnalyze(asset, true, e)}
              onToggleDetails={(e) => selected === asset.name ? closePopup() : openPopup(asset, e)}
              onAddToDraft={() => handleAddToDraft(asset)}
              onSetMain={() => handleSetMain(asset)}
              onStripAudio={() => handleStripAudio(asset)}
              onSplitShots={(e) => handleSplitShots(asset, !!shotsMap[asset.name], e)}
              onDelete={() => handleDelete(asset)}
            />
          ))}
        </div>
        {filtered.length === 0 && (
          <div className="flex-1 flex flex-col items-center justify-center text-[#121212]/50 py-16">
            <Search size={40} className="mb-4 opacity-20" strokeWidth={1} />
            <p className="font-serif italic text-2xl">No matching assets</p>
            <p className="text-[10px] uppercase tracking-widest mt-2">Try a different search or filter</p>
          </div>
        )}
        </>
      )}

      {/* 就地 popup: 查看输出浮在被点卡片旁, 不再在网格底部展开 */}
      {selectedAsset && (
        <AssetDetailPopup
          asset={selectedAsset}
          shots={shotsMap[selectedAsset.name]}
          anchor={popupAnchor}
          onClose={closePopup}
        />
      )}
    </div>
  );
}

/** 单张素材卡片 —— 抽成组件便于用 useInView 做视口内懒加载.
 * 视频: 视口外只渲染占位灰底; 进入视口拉一张 thumbnail jpg; hover 才挂 <video> 播放.
 * 图片: loading="lazy" 原生懒加载, 浏览器自动按视口距离决定何时请求. */
interface CardProps {
  asset: Asset;
  selected: boolean;
  selectMode: boolean;         // 多选批量模式: 卡片点击切换选中而不是打开详情
  checked: boolean;
  onToggleCheck: () => void;
  soundOn: boolean;
  mainVideoName?: string;
  analyzing: boolean;
  splitting: boolean;
  deleting: boolean;
  strippingAudio: boolean;
  settingMain: boolean;
  adding: boolean;
  hasShots: boolean;
  draftId: string | null;
  onHover: (e: React.MouseEvent, path: string) => void;
  onLeave: (e: React.MouseEvent, path: string) => void;
  onMeta: (asset: Asset, e: React.SyntheticEvent<HTMLVideoElement>) => void;
  onAnalyze: (e: React.MouseEvent) => void;
  onReanalyze: (e: React.MouseEvent) => void;
  onToggleDetails: (e: React.MouseEvent) => void;
  onAddToDraft: () => void;
  onSetMain: () => void;
  onStripAudio: () => void;
  onSplitShots: (e: React.MouseEvent) => void;
  onDelete: () => void;
}

function AssetCard({
  asset, selected, selectMode, checked, onToggleCheck, soundOn, mainVideoName,
  analyzing, splitting, deleting, strippingAudio, settingMain, adding, hasShots, draftId,
  onHover, onLeave, onMeta,
  onAnalyze, onReanalyze, onToggleDetails, onAddToDraft, onSetMain, onStripAudio, onSplitShots, onDelete,
}: CardProps) {
  const [ref, inView] = useInView<HTMLDivElement>();
  const [hovering, setHovering] = useState(false);
  const ANALYZABLE: AssetType[] = ['video', 'image'];

  return (
    <div ref={ref} onClick={selectMode ? onToggleCheck : undefined}
      className={`border overflow-hidden group flex flex-col transition-colors cursor-pointer
        ${selectMode && checked ? 'border-[#121212] bg-[#121212]/5' : selected ? 'border-[#121212]' : 'border-[#121212]/10 hover:border-[#121212]/30'}`}>
      {selectMode && (
        <div className={`absolute top-2 right-2 z-20 w-6 h-6 flex items-center justify-center border-2 transition-colors ${checked ? 'bg-[#121212] border-[#121212] text-[#FDFCF8]' : 'bg-white/70 border-[#121212]/40'}`}>
          {checked && <Check size={14} strokeWidth={3} />}
        </div>
      )}
      <div className={`${asset._portrait ? 'h-72' : 'h-48'} border-b border-[#121212]/10 flex items-center justify-center relative bg-[#121212]`}
        onMouseEnter={asset.type === 'video' ? (e) => { setHovering(true); onHover(e, asset.path); }
          : asset.type === 'image' ? () => setHovering(true) : undefined}
        onMouseLeave={asset.type === 'video' ? (e) => { setHovering(false); onLeave(e, asset.path); }
          : asset.type === 'image' ? () => setHovering(false) : undefined}>
        {asset.type === 'video' ? (
          <>
            {/* 视口外: 仅占位图标, 零请求; 进入视口: 挂缩略图 jpg; hover: 换成 <video> 播放 */}
            {inView && hovering ? (
              <video className={`absolute inset-0 w-full h-full ${asset._portrait ? 'object-contain' : 'object-cover'}`}
                src={api.serveUrl(asset.path)} autoPlay muted={!soundOn} playsInline loop
                onLoadedMetadata={(e) => onMeta(asset, e)} />
            ) : inView ? (
              <img className={`absolute inset-0 w-full h-full ${asset._portrait ? 'object-contain' : 'object-cover'}`}
                src={api.thumbnailUrl(asset.path)} alt={asset.name} loading="lazy" />
            ) : (
              <div className="absolute inset-0 flex items-center justify-center">
                <FileVideo size={32} strokeWidth={1} className="text-white/20" />
              </div>
            )}
            <div className="absolute inset-0 bg-[#FDFCF8]/80 opacity-0 group-hover:opacity-0 flex items-center justify-center pointer-events-none">
              <Play size={20} strokeWidth={1.5} className="ml-1 text-[#121212]" />
            </div>
            {asset._duration && (
              <div className="absolute bottom-2 right-2 bg-[#121212] text-[#FDFCF8] text-[9px] px-2 py-0.5 tracking-widest">{asset._duration}</div>
            )}
            {asset._cached && (
              <div className="absolute bottom-2 left-2 bg-[#121212]/70 text-[#FDFCF8] text-[8px] px-1.5 py-0.5 uppercase tracking-widest">cached</div>
            )}
            {asset.has_audio === false && (
              <div className="absolute top-2 left-2 bg-emerald-700/90 text-white text-[8px] px-1.5 py-0.5 uppercase tracking-widest flex items-center gap-1">
                <VolumeX size={10} strokeWidth={2} /> No Audio
              </div>
            )}
            {mainVideoName === asset.name && (
              <div className="absolute top-2 right-2 bg-amber-600/90 text-white text-[8px] px-1.5 py-0.5 uppercase tracking-widest flex items-center gap-1">
                <Star size={10} strokeWidth={2} fill="currentColor" /> Main
              </div>
            )}
          </>
        ) : asset.type === 'image' ? (
          <>
            <img className="absolute inset-0 w-full h-full object-contain" src={api.serveUrl(asset.path)} alt={asset.name} loading="lazy" />
            {/* hover 大图预览: 居中浮层, pointer-events-none 不会打断 hover 状态 */}
            {hovering && (
              <div className="fixed inset-0 z-40 flex items-center justify-center pointer-events-none bg-[#121212]/60"
                style={{ animation: 'fadeIn 120ms ease-out' }}>
                <img className="max-w-[80vw] max-h-[80vh] object-contain shadow-2xl"
                  src={api.serveUrl(asset.path)} alt={asset.name} />
                <div className="absolute bottom-6 left-1/2 -translate-x-1/2 bg-[#121212]/80 text-[#FDFCF8] text-[10px] uppercase tracking-widest px-4 py-1.5 max-w-[70vw] truncate">
                  {asset.name} · {formatSize(asset.size)}
                </div>
              </div>
            )}
          </>
        ) : asset.type === 'audio' ? (
          <div className="flex flex-col items-center gap-3 px-4">
            <Music size={32} strokeWidth={1} className="text-white/30" />
            <audio controls className="w-full max-w-[200px]" src={api.serveUrl(asset.path)} preload="none" />
          </div>
        ) : getIcon(asset.type)}
      </div>
      <div className="p-6 flex-1 flex flex-col">
        <h3 className="font-serif text-lg italic truncate mb-2" title={asset.name}>{asset.name}</h3>
        <div className="flex items-center justify-between text-[10px] uppercase tracking-widest opacity-50 mb-6">
          <span>{asset.type}</span>
          <span>{formatSize(asset.size)}</span>
        </div>
        <div className="flex gap-2 mt-auto" onClick={selectMode ? (e) => e.stopPropagation() : undefined}>
          {ANALYZABLE.includes(asset.type) && (
            <>
              <button onClick={onAnalyze} disabled={analyzing}
                className="flex-1 px-3 py-2 border border-[#121212]/20 text-[#121212] text-[10px] uppercase tracking-widest font-bold hover:bg-[#121212]/5 transition-colors disabled:opacity-50 flex items-center justify-center gap-1">
                {analyzing ? <Loader2 size={12} strokeWidth={1.5} className="animate-spin" /> : null}
                {asset.analysis && !('error' in (asset.analysis as any)) ? 'View' : 'Analyze'}
              </button>
              {asset.analysis && !('error' in (asset.analysis as any)) && (
                <button onClick={onReanalyze} disabled={analyzing}
                  className="px-3 py-2 border border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5 transition-colors disabled:opacity-50" title="重新分析 (忽略缓存)">
                  {analyzing ? <Loader2 size={14} strokeWidth={1.5} className="animate-spin" /> : <RefreshCw size={14} strokeWidth={1.5} />}
                </button>
              )}
            </>
          )}
          <button onClick={onToggleDetails}
            className={`px-3 py-2 border text-[#121212] hover:bg-[#121212]/5 transition-colors ${selected ? 'border-[#121212] bg-[#121212]/5' : 'border-[#121212]/20'}`} title="Details">
            <Info size={14} strokeWidth={1.5} />
          </button>
          {['video', 'audio', 'image'].includes(asset.type) && (
            <button onClick={onAddToDraft} disabled={adding || !draftId}
              className="px-3 py-2 border border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
              title={draftId ? `添加到激活草稿 (${draftId.slice(0, 12)}…)` : '无激活草稿 — 先在 Drafts 新建/打开草稿'}>
              {adding ? <Loader2 size={14} strokeWidth={1.5} className="animate-spin" /> : <Plus size={14} strokeWidth={1.5} />}
            </button>
          )}
          {asset.type === 'video' && (
            <button onClick={onSetMain} disabled={settingMain || mainVideoName === asset.name}
              className={`px-3 py-2 border transition-colors disabled:opacity-50 ${mainVideoName === asset.name ? 'border-amber-600/40 text-amber-700 bg-amber-50' : 'border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5'}`}
              title={mainVideoName === asset.name ? '当前主视频' : '设为主视频'}>
              {settingMain ? <Loader2 size={14} strokeWidth={1.5} className="animate-spin" /> : <Star size={14} strokeWidth={1.5} fill={mainVideoName === asset.name ? 'currentColor' : 'none'} />}
            </button>
          )}
          {asset.type === 'video' && (
            <button onClick={onStripAudio} disabled={strippingAudio || asset.has_audio === false}
              className={`px-3 py-2 border transition-colors disabled:opacity-50 ${asset.has_audio === false ? 'border-emerald-700/30 text-emerald-700 bg-emerald-50' : 'border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5'}`}
              title={asset.has_audio === false ? '已去除音轨' : '去除声音 (之后只用画面匹配, 不再走 ASR)'}>
              {strippingAudio ? <Loader2 size={14} strokeWidth={1.5} className="animate-spin" /> : <VolumeX size={14} strokeWidth={1.5} />}
            </button>
          )}
          {asset.type === 'video' && (
            <button onClick={onSplitShots} disabled={splitting}
              className={`px-3 py-2 border transition-colors disabled:opacity-50 ${hasShots ? 'border-emerald-700/30 text-emerald-700 bg-emerald-50' : 'border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5'}`}
              title={hasShots ? `已拆出镜头 (点击重新拆分)` : '分镜拆分 (检测镜头边界, 切成独立小视频)'}>
              {splitting ? <Loader2 size={14} strokeWidth={1.5} className="animate-spin" /> : <Scissors size={14} strokeWidth={1.5} />}
            </button>
          )}
          <button onClick={onDelete} disabled={deleting}
            className="px-3 py-2 border border-[#121212]/20 text-[#121212] hover:bg-red-50 hover:border-red-700/30 hover:text-red-700 transition-colors disabled:opacity-50" title="Delete">
            {deleting ? <Loader2 size={14} strokeWidth={1.5} className="animate-spin" /> : <Trash2 size={14} strokeWidth={1.5} />}
          </button>
        </div>
      </div>
    </div>
  );
}
