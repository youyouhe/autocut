import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { X, Play } from 'lucide-react';
import type { Asset, PerceiveResult, Shot } from '../api';

function getVisualJson(a: PerceiveResult | null | undefined) {
  if (!a?.visual_analysis) return {};
  try { const m = a.visual_analysis.match(/\{[\s\S]*\}/); return m ? JSON.parse(m[0]) : {}; }
  catch { return {}; }
}

interface Props {
  asset: Asset;
  shots: Shot[] | null | undefined;
  anchor: DOMRect | null;
  onClose: () => void;
}

/**
 * 素材"查看"输出就地弹层: 浮在被点卡片附近 (锚点定位 + 视口夹取), 不再渲染在网格底部
 * 让用户翻页. 内容 = 分镜网格 + 分析块, 与原 AssetPanel 下方输出一致.
 * 实时: asset / shots 由 AssetPanel 从 props 透传, 后台分析结果回填 setAssets 后
 * 本组件拿到的就是最新 analysis —— 无需额外刷新.
 */
export default function AssetDetailPopup({ asset, shots, anchor, onClose }: Props) {
  const popRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number; width: number } | null>(null);

  const analysis = asset.analysis as PerceiveResult | { error?: string } | null | undefined;
  const vj = analysis && !('error' in analysis) ? getVisualJson(analysis as PerceiveResult) : {};
  const audioText = analysis && !('error' in analysis) ? (analysis as PerceiveResult).audio?.full_text : '';

  // 锚点定位 + 视口夹取: 优先放卡片右侧, 右侧放不下放左侧; 上下做 min/max 夹取不溢出视口.
  useLayoutEffect(() => {
    if (!anchor) return;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const gap = 12;
    const width = Math.min(440, vw - gap * 2);
    // 先估算高度上限, 实际由内容撑开 + max-h 控制
    const placeRight = anchor.right + gap + width <= vw;
    const left = placeRight
      ? anchor.right + gap
      : Math.max(gap, anchor.left - gap - width);
    // 垂直: 顶部对齐卡片顶, 但不超出视口
    const top = Math.max(gap, Math.min(anchor.top, vh - gap - 320));
    setPos({ left, top, width });
  }, [anchor]);

  // Escape 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  if (!pos) {
    // 第一帧先不定位 (等 layoutEffect), 但仍要渲染背板以免穿透
    return <div className="fixed inset-0 z-40" onClick={onClose} />;
  }

  return (
    <>
      {/* 背板 click-catcher (透明, 不挡视线) */}
      <div className="fixed inset-0 z-40" onClick={onClose} />
      <div
        ref={popRef}
        className="fixed z-50 bg-[#FDFCF8] border border-[#121212]/30 shadow-2xl flex flex-col overflow-hidden"
        style={{ left: pos.left, top: pos.top, width: pos.width, maxHeight: '80vh' }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* 标题栏 */}
        <div className="flex items-center justify-between border-b border-[#121212]/10 px-5 py-3 bg-[#121212]/3">
          <div className="font-serif italic text-lg truncate pr-3" title={asset.name}>{asset.name}</div>
          <button onClick={onClose} className="opacity-50 hover:opacity-100 transition-opacity flex-shrink-0" title="关闭 (Esc)">
            <X size={18} strokeWidth={1.5} />
          </button>
        </div>

        <div className="overflow-y-auto p-5 space-y-5">
          {/* 分镜拆分结果 */}
          {shots && shots.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-widest font-bold opacity-60 mb-3">
                Shots · {shots.length} 个镜头
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                {shots.map(shot => (
                  <div key={shot.index} className="border border-[#121212]/10">
                    <div className={`${asset._portrait ? 'h-40' : 'h-24'} bg-[#121212] relative overflow-hidden`}>
                      {shot.clip_url ? (
                        <video className={`absolute inset-0 w-full h-full ${asset._portrait ? 'object-contain' : 'object-cover'}`} src={shot.clip_url}
                          poster={shot.keyframe_url || undefined} controls preload="none" />
                      ) : shot.keyframe_url ? (
                        <img className={`absolute inset-0 w-full h-full ${asset._portrait ? 'object-contain' : 'object-cover'}`} src={shot.keyframe_url} alt={`shot ${shot.index}`} />
                      ) : (
                        <div className="absolute inset-0 flex items-center justify-center"><Play size={16} className="text-white/30" strokeWidth={1} /></div>
                      )}
                    </div>
                    <div className="px-2 py-1 text-[9px] uppercase tracking-widest opacity-60 flex justify-between">
                      <span>#{shot.index}</span>
                      <span>{shot.duration.toFixed(1)}s</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 分析结果 */}
          {analysis ? (
            <div>
              <div className="text-[10px] uppercase tracking-widest font-bold opacity-60 mb-3">Analysis</div>
              {'error' in analysis ? (
                <div className="text-red-700 text-sm font-mono">{(analysis as any).error}</div>
              ) : (
                <div className="space-y-3 text-sm">
                  <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] uppercase tracking-widest opacity-60">
                    {(analysis as PerceiveResult).meta && (
                      <span>{(analysis as PerceiveResult).meta!.width}×{(analysis as PerceiveResult).meta!.height}</span>
                    )}
                    {asset.type === 'video' && (
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
          ) : (
            <p className="text-sm opacity-50 font-light">暂无分析结果 — 点"Analyze"开始分析。</p>
          )}
        </div>
      </div>
    </>
  );
}
