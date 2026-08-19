import { useState, useRef, useEffect } from 'react';
import { Upload, Play, Info, FileVideo, Image as ImageIcon, Music, RefreshCw, Headset, VolumeOff, VolumeX, Loader2, Trash2, Captions, FileText, Scissors, Star } from 'lucide-react';
import * as api from '../api';
import type { Asset, AssetType, PerceiveResult, Shot, MainVideo } from '../api';

interface Props {
  assets: Asset[];
  setAssets: React.Dispatch<React.SetStateAction<Asset[]>>;
  refreshAssets: () => Promise<void>;
}

const playbackMemory: Record<string, number> = {};

function getVisualJson(a: PerceiveResult | null | undefined) {
  if (!a?.visual_analysis) return {};
  try { const m = a.visual_analysis.match(/\{[\s\S]*\}/); return m ? JSON.parse(m[0]) : {}; }
  catch { return {}; }
}

export default function AssetPanel({ assets, setAssets, refreshAssets }: Props) {
  const [uploading, setUploading] = useState(false);
  const [analyzing, setAnalyzing] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [strippingAudio, setStrippingAudio] = useState<string | null>(null);
  const [splitting, setSplitting] = useState<string | null>(null);
  const [shotsMap, setShotsMap] = useState<Record<string, Shot[] | null>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [soundOn, setSoundOn] = useState(false);
  const [toast, setToast] = useState<{ msg: string; type: 'success' | 'error' } | null>(null);
  const [mainVideo, setMainVideoState] = useState<MainVideo | null>(null);
  const [mainVideoPortrait, setMainVideoPortrait] = useState(false);
  const [settingMain, setSettingMain] = useState<string | null>(null);
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

  const formatSize = (bytes?: number) => bytes ? (bytes / 1048576).toFixed(2) + ' MB' : '';

  const getIcon = (type: string, size = 32) => {
    if (type === 'video') return <FileVideo size={size} strokeWidth={1} className="text-white/30" />;
    if (type === 'image') return <ImageIcon size={size} strokeWidth={1} className="text-white/30" />;
    if (type === 'audio') return <Music size={size} strokeWidth={1} className="text-white/30" />;
    if (type === 'subtitle') return <Captions size={size} strokeWidth={1} className="text-white/30" />;
    if (type === 'text') return <FileText size={size} strokeWidth={1} className="text-white/30" />;
    return <FileVideo size={size} strokeWidth={1} className="text-white/30" />;
  };

  const ANALYZABLE: AssetType[] = ['video', 'image'];

  const handleAnalyze = async (asset: Asset, force = false) => {
    if (!ANALYZABLE.includes(asset.type)) return;
    setSelected(asset.name);
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

  const handleSplitShots = async (asset: Asset, force = false) => {
    setSelected(asset.name);
    setSplitting(asset.name);
    try {
      const r = await api.splitShots(asset.name, { force });
      if (r.ok && r.shots) {
        setShotsMap(prev => ({ ...prev, [asset.name]: r.shots! }));
        // 切出来的镜头视频已作为素材落到资产目录, 刷新列表让它自动出现在资产里
        await refreshAssets();
        showToast(`拆出 ${r.shots.length} 个镜头`, 'success');
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
      if (selected === asset.name) setSelected(null);
    } catch (err: any) {
      alert('删除失败: ' + err.message);
    } finally { setDeleting(null); }
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

  const selectedAsset = assets.find(a => a.name === selected) || null;
  const analysis = selectedAsset?.analysis as PerceiveResult | { error?: string } | null | undefined;
  const vj = analysis && !('error' in analysis) ? getVisualJson(analysis as PerceiveResult) : {};
  const audioText = analysis && !('error' in analysis) ? (analysis as PerceiveResult).audio?.full_text : '';

  return (
    <div className="h-full w-full flex flex-col p-12 overflow-y-auto relative">
      {toast && (
        <div className={`fixed top-6 right-6 z-50 px-4 py-3 text-sm shadow-lg ${toast.type === 'success' ? 'bg-emerald-700 text-white' : 'bg-red-700 text-white'}`}>
          {toast.msg}
        </div>
      )}
      <div className="flex items-center justify-between mb-12 border-b border-[#121212]/10 pb-6">
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
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-8">
          {assets.map((asset) => (
            <div key={asset.path} className={`border overflow-hidden group flex flex-col transition-colors ${selected === asset.name ? 'border-[#121212]' : 'border-[#121212]/10 hover:border-[#121212]/30'}`}>
              <div className={`${asset._portrait ? 'h-72' : 'h-48'} border-b border-[#121212]/10 flex items-center justify-center relative bg-[#121212]`}
                onMouseEnter={asset.type === 'video' ? (e) => onVideoHover(e, asset.path) : undefined}
                onMouseLeave={asset.type === 'video' ? (e) => onVideoLeave(e, asset.path) : undefined}>
                {asset.type === 'video' ? (
                  <>
                    <video className={`absolute inset-0 w-full h-full ${asset._portrait ? 'object-contain' : 'object-cover'}`} src={api.serveUrl(asset.path)} preload="metadata" muted={!soundOn} playsInline
                      onLoadedMetadata={(e) => onVideoMeta(asset, e)} />
                    <div className="absolute inset-0 bg-[#FDFCF8]/80 opacity-0 group-hover:opacity-0 flex items-center justify-center">
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
                    {mainVideo?.name === asset.name && (
                      <div className="absolute top-2 right-2 bg-amber-600/90 text-white text-[8px] px-1.5 py-0.5 uppercase tracking-widest flex items-center gap-1">
                        <Star size={10} strokeWidth={2} fill="currentColor" /> Main
                      </div>
                    )}
                  </>
                ) : asset.type === 'image' ? (
                  <img className="absolute inset-0 w-full h-full object-contain" src={api.serveUrl(asset.path)} alt={asset.name} />
                ) : asset.type === 'audio' ? (
                  <div className="flex flex-col items-center gap-3 px-4">
                    <Music size={32} strokeWidth={1} className="text-white/30" />
                    <audio controls className="w-full max-w-[200px]" src={api.serveUrl(asset.path)} />
                  </div>
                ) : getIcon(asset.type)}
              </div>
              <div className="p-6 flex-1 flex flex-col">
                <h3 className="font-serif text-lg italic truncate mb-2" title={asset.name}>{asset.name}</h3>
                <div className="flex items-center justify-between text-[10px] uppercase tracking-widest opacity-50 mb-6">
                  <span>{asset.type}</span>
                  <span>{formatSize(asset.size)}</span>
                </div>
                <div className="flex gap-2 mt-auto">
                  {ANALYZABLE.includes(asset.type) && (
                    <>
                      <button onClick={() => handleAnalyze(asset)} disabled={analyzing === asset.name}
                        className="flex-1 px-3 py-2 border border-[#121212]/20 text-[#121212] text-[10px] uppercase tracking-widest font-bold hover:bg-[#121212]/5 transition-colors disabled:opacity-50 flex items-center justify-center gap-1">
                        {analyzing === asset.name ? <Loader2 size={12} strokeWidth={1.5} className="animate-spin" /> : null}
                        {asset.analysis && !('error' in (asset.analysis as any)) ? 'View' : 'Analyze'}
                      </button>
                      {asset.analysis && !('error' in (asset.analysis as any)) && (
                        <button onClick={() => handleAnalyze(asset, true)} disabled={analyzing === asset.name}
                          className="px-3 py-2 border border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5 transition-colors disabled:opacity-50" title="重新分析 (忽略缓存)">
                          {analyzing === asset.name ? <Loader2 size={14} strokeWidth={1.5} className="animate-spin" /> : <RefreshCw size={14} strokeWidth={1.5} />}
                        </button>
                      )}
                    </>
                  )}
                  <button onClick={() => setSelected(selected === asset.name ? null : asset.name)}
                    className={`px-3 py-2 border text-[#121212] hover:bg-[#121212]/5 transition-colors ${selected === asset.name ? 'border-[#121212] bg-[#121212]/5' : 'border-[#121212]/20'}`} title="Details">
                    <Info size={14} strokeWidth={1.5} />
                  </button>
                  {asset.type === 'video' && (
                    <button onClick={() => handleSetMain(asset)} disabled={settingMain === asset.name || mainVideo?.name === asset.name}
                      className={`px-3 py-2 border transition-colors disabled:opacity-50 ${mainVideo?.name === asset.name ? 'border-amber-600/40 text-amber-700 bg-amber-50' : 'border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5'}`}
                      title={mainVideo?.name === asset.name ? '当前主视频' : '设为主视频'}>
                      {settingMain === asset.name ? <Loader2 size={14} strokeWidth={1.5} className="animate-spin" /> : <Star size={14} strokeWidth={1.5} fill={mainVideo?.name === asset.name ? 'currentColor' : 'none'} />}
                    </button>
                  )}
                  {asset.type === 'video' && (
                    <button onClick={() => handleStripAudio(asset)} disabled={strippingAudio === asset.name || asset.has_audio === false}
                      className={`px-3 py-2 border transition-colors disabled:opacity-50 ${asset.has_audio === false ? 'border-emerald-700/30 text-emerald-700 bg-emerald-50' : 'border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5'}`}
                      title={asset.has_audio === false ? '已去除音轨' : '去除声音 (之后只用画面匹配, 不再走 ASR)'}>
                      {strippingAudio === asset.name ? <Loader2 size={14} strokeWidth={1.5} className="animate-spin" /> : <VolumeX size={14} strokeWidth={1.5} />}
                    </button>
                  )}
                  {asset.type === 'video' && (
                    <button onClick={() => handleSplitShots(asset, !!shotsMap[asset.name])} disabled={splitting === asset.name}
                      className={`px-3 py-2 border transition-colors disabled:opacity-50 ${shotsMap[asset.name] ? 'border-emerald-700/30 text-emerald-700 bg-emerald-50' : 'border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5'}`}
                      title={shotsMap[asset.name] ? `已拆出 ${shotsMap[asset.name]!.length} 个镜头 (点击重新拆分)` : '分镜拆分 (检测镜头边界, 切成独立小视频)'}>
                      {splitting === asset.name ? <Loader2 size={14} strokeWidth={1.5} className="animate-spin" /> : <Scissors size={14} strokeWidth={1.5} />}
                    </button>
                  )}
                  <button onClick={() => handleDelete(asset)} disabled={deleting === asset.name}
                    className="px-3 py-2 border border-[#121212]/20 text-[#121212] hover:bg-red-50 hover:border-red-700/30 hover:text-red-700 transition-colors disabled:opacity-50" title="Delete">
                    {deleting === asset.name ? <Loader2 size={14} strokeWidth={1.5} className="animate-spin" /> : <Trash2 size={14} strokeWidth={1.5} />}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 分镜拆分结果 (选中卡片下方) */}
      {selectedAsset && shotsMap[selectedAsset.name] && shotsMap[selectedAsset.name]!.length > 0 && (
        <div className="mt-8 border border-[#121212]/10 p-6 bg-white/50">
          <div className="text-[10px] uppercase tracking-widest font-bold opacity-60 mb-4">
            Shots · {selectedAsset.name} ({shotsMap[selectedAsset.name]!.length} 个镜头)
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-4">
            {shotsMap[selectedAsset.name]!.map(shot => (
              <div key={shot.index} className="border border-[#121212]/10">
                <div className={`${selectedAsset._portrait ? 'h-72' : 'h-48'} bg-[#121212] relative overflow-hidden`}>
                  {shot.clip_url ? (
                    <video className={`absolute inset-0 w-full h-full ${selectedAsset._portrait ? 'object-contain' : 'object-cover'}`} src={shot.clip_url}
                      poster={shot.keyframe_url || undefined} controls preload="none" />
                  ) : shot.keyframe_url ? (
                    <img className={`absolute inset-0 w-full h-full ${selectedAsset._portrait ? 'object-contain' : 'object-cover'}`} src={shot.keyframe_url} alt={`shot ${shot.index}`} />
                  ) : (
                    <div className="absolute inset-0 flex items-center justify-center"><Play size={16} className="text-white/30" strokeWidth={1} /></div>
                  )}
                </div>
                <div className="px-2 py-1.5 text-[9px] uppercase tracking-widest opacity-60 flex justify-between">
                  <span>#{shot.index}</span>
                  <span>{shot.duration.toFixed(1)}s</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 分析结果 (选中卡片下方) */}
      {selectedAsset && analysis && (
        <div className="mt-8 border border-[#121212]/10 p-6 bg-white/50">
          <div className="text-[10px] uppercase tracking-widest font-bold opacity-60 mb-4">Analysis · {selectedAsset.name}</div>
          {'error' in analysis ? (
            <div className="text-red-700 text-sm font-mono">{(analysis as any).error}</div>
          ) : (
            <div className="space-y-3 text-sm">
              <div className="flex gap-4 text-[10px] uppercase tracking-widest opacity-60">
                {(analysis as PerceiveResult).meta && (
                  <span>{(analysis as PerceiveResult).meta!.width}×{(analysis as PerceiveResult).meta!.height}</span>
                )}
                {selectedAsset.type === 'video' && (
                  <span>{((analysis as PerceiveResult).meta?.duration ?? 0).toFixed(1)}s</span>
                )}
                {(analysis as PerceiveResult).scenes && (analysis as PerceiveResult).scenes!.length > 0 && (
                  <span>{(analysis as PerceiveResult).scenes!.length} scenes</span>
                )}
                {(analysis as PerceiveResult)._cached && <span className="text-amber-700">cached</span>}
                {(analysis as PerceiveResult).analysis_mode && (
                  <span className="text-blue-700">via {(analysis as PerceiveResult).analysis_mode}</span>
                )}
              </div>
              {vj.content && <p className="font-light leading-relaxed">🎬 {vj.content}</p>}
              {(analysis as PerceiveResult).tags && (analysis as PerceiveResult).tags!.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {(analysis as PerceiveResult).tags!.map((t, i) => (
                    <span key={i} className="text-[10px] px-2 py-0.5 bg-[#121212]/8 border border-[#121212]/10 tracking-wide">{t}</span>
                  ))}
                </div>
              )}
              {vj.mood && <p className="font-light leading-relaxed opacity-70">🎭 {vj.mood}</p>}
              {vj.quality && <p className="font-light leading-relaxed opacity-70">⭐ {vj.quality}</p>}
              {audioText && (
                <div>
                  <div className="text-[10px] uppercase tracking-widest opacity-50 mb-1">Transcript</div>
                  <p className="font-light leading-relaxed opacity-80">{audioText}</p>
                </div>
              )}
              {(analysis as PerceiveResult).scenes && (analysis as PerceiveResult).scenes!.length > 0 && (
                <div>
                  <div className="text-[10px] uppercase tracking-widest opacity-50 mb-1">Scene Cuts</div>
                  <p className="font-mono text-xs opacity-70">{(analysis as PerceiveResult).scenes!.map(t => `${t.toFixed(1)}s`).join(' · ')}</p>
                </div>
              )}
              {(analysis as PerceiveResult).srt && (
                <div>
                  <div className="text-[10px] uppercase tracking-widest opacity-50 mb-1">SRT Subtitles</div>
                  <pre className="font-mono text-[11px] leading-relaxed bg-[#121212]/5 p-3 overflow-x-auto max-h-48 overflow-y-auto whitespace-pre-wrap">{(analysis as PerceiveResult).srt}</pre>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
