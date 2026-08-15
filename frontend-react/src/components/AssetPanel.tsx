import { useState, useRef } from 'react';
import { Upload, Play, Info, FileVideo, Image as ImageIcon, Music, RefreshCw, Headset, VolumeOff, Loader2 } from 'lucide-react';
import * as api from '../api';
import type { Asset, PerceiveResult } from '../api';

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
  const [selected, setSelected] = useState<string | null>(null);
  const [soundOn, setSoundOn] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

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
    if (type === 'video') return <FileVideo size={size} strokeWidth={1} className="text-[#121212]/40" />;
    if (type === 'image') return <ImageIcon size={size} strokeWidth={1} className="text-[#121212]/40" />;
    if (type === 'audio') return <Music size={size} strokeWidth={1} className="text-[#121212]/40" />;
    return <FileVideo size={size} strokeWidth={1} className="text-[#121212]/40" />;
  };

  const handleAnalyze = async (asset: Asset, force = false) => {
    if (asset.type !== 'video') return;
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

  const onVideoHover = (e: React.MouseEvent, asset: Asset) => {
    const v = (e.currentTarget as HTMLElement).querySelector('video'); if (!v) return;
    const saved = playbackMemory[asset.path]; if (saved) v.currentTime = saved;
    v.play().catch(() => {});
  };
  const onVideoLeave = (e: React.MouseEvent, asset: Asset) => {
    const v = (e.currentTarget as HTMLElement).querySelector('video'); if (!v) return;
    playbackMemory[asset.path] = v.currentTime; v.pause();
  };
  const onVideoMeta = (asset: Asset, e: React.SyntheticEvent<HTMLVideoElement>) => {
    const d = e.currentTarget.duration; if (!d || !isFinite(d)) return;
    const m = Math.floor(d / 60), s = Math.floor(d % 60);
    const dur = m > 0 ? `${m}:${String(s).padStart(2, '0')}` : `${Math.round(d)}s`;
    setAssets(prev => prev.map(a => a.path === asset.path ? { ...a, _duration: dur } : a));
  };

  const selectedAsset = assets.find(a => a.name === selected) || null;
  const analysis = selectedAsset?.analysis as PerceiveResult | { error?: string } | null | undefined;
  const vj = analysis && !('error' in analysis) ? getVisualJson(analysis as PerceiveResult) : {};
  const audioText = analysis && !('error' in analysis) ? (analysis as PerceiveResult).audio?.full_text : '';

  return (
    <div className="h-full w-full flex flex-col p-12 overflow-y-auto">
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
            accept=".mp4,.mov,.avi,.mkv,.jpg,.png,.jpeg,.mp3,.wav,.aac,.m4a" />
        </div>
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
              <div className="h-48 border-b border-[#121212]/10 flex items-center justify-center relative bg-[#121212]/5"
                onMouseEnter={asset.type === 'video' ? (e) => onVideoHover(e, asset) : undefined}
                onMouseLeave={asset.type === 'video' ? (e) => onVideoLeave(e, asset) : undefined}>
                {asset.type === 'video' ? (
                  <>
                    <video className="absolute inset-0 w-full h-full object-cover" src={api.serveUrl(asset.path)} preload="metadata" muted={!soundOn} playsInline
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
                  </>
                ) : getIcon(asset.type)}
              </div>
              <div className="p-6 flex-1 flex flex-col">
                <h3 className="font-serif text-lg italic truncate mb-2" title={asset.name}>{asset.name}</h3>
                <div className="flex items-center justify-between text-[10px] uppercase tracking-widest opacity-50 mb-6">
                  <span>{asset.type}</span>
                  <span>{formatSize(asset.size)}</span>
                </div>
                <div className="flex gap-2 mt-auto">
                  {asset.type === 'video' && (
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
                </div>
              </div>
            </div>
          ))}
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
                <span>{((analysis as PerceiveResult).meta?.duration ?? 0).toFixed(1)}s</span>
                {(analysis as PerceiveResult).scenes && (analysis as PerceiveResult).scenes!.length > 0 && (
                  <span>{(analysis as PerceiveResult).scenes!.length} scenes</span>
                )}
                {(analysis as PerceiveResult)._cached && <span className="text-amber-700">cached</span>}
              </div>
              {vj.content && <p className="font-light leading-relaxed">🎬 {vj.content}</p>}
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
