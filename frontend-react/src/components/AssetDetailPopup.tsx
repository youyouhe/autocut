import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { X, Play, GripVertical } from 'lucide-react';
import type { Asset, PerceiveResult, Shot } from '../api';

function getVisualJson(a: PerceiveResult | null | undefined) {
  if (!a?.visual_analysis) return {};
  try { const m = a.visual_analysis.match(/\{[\s\S]*\}/); return m ? JSON.parse(m[0]) : {}; }
  catch { return {}; }
}

/** JSON 解析失败 (如 VLM 输出被 max_tokens 截断没闭合) 时, 退回展示原文, 不留空白. */
function getVisualFallback(a: PerceiveResult | null | undefined): string {
  const v = a?.visual_analysis;
  if (!v) return '';
  try { const m = v.match(/\{[\s\S]*\}/); return m && JSON.parse(m[0]) ? '' : v; }
  catch { return v; }
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
 *
 * 可拖动: 标题栏就是拖动柄 (pointer capture), 拖动全程把窗口夹在视口内, 保证不甩丢;
 * 用户一旦手动拖过就停用自动定位, 位置完全交给用户.
 * 打开时还会按内容真实高度自动把 top 上移夹进视口 —— 修复内容超高时底部被裁"显示不全".
 */
export default function AssetDetailPopup({ asset, shots, anchor, onClose }: Props) {
  const popRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number; width: number } | null>(null);
  const GAP = 12;

  // 用户手动拖过 → 停止自动追位, 避免自动夹取跟用户抢位置.
  const autoPlaced = useRef(false);
  // 拖动会话基线: pointerdown 时记录的起点 + 初始矩形.
  const dragRef = useRef<{ x: number; y: number; left: number; top: number; width: number; height: number } | null>(null);

  const analysis = asset.analysis as PerceiveResult | { error?: string } | null | undefined;
  const vj = analysis && !('error' in analysis) ? getVisualJson(analysis as PerceiveResult) : {};
  const vFallback = analysis && !('error' in analysis) ? getVisualFallback(analysis as PerceiveResult) : '';
  const audioText = analysis && !('error' in analysis) ? (analysis as PerceiveResult).audio?.full_text : '';

  // 阶段一 — 锚点定位: 优先放卡片右侧, 右侧放不下放左侧. top 先用卡片位置粗估
  // (内容真实高度此时未知), 待元素量出高度后由阶段二按实际高度精修.
  useLayoutEffect(() => {
    if (!anchor) return;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const width = Math.min(440, vw - GAP * 2);
    const placeRight = anchor.right + GAP + width <= vw;
    const left = placeRight
      ? anchor.right + GAP
      : Math.max(GAP, anchor.left - GAP - width);
    // 垂直: 顶部对齐卡片顶, 先粗夹在视口内(留 320 给下方内容), 量出真实高度后再精修
    const top = Math.max(GAP, Math.min(anchor.top, vh - GAP - 320));
    autoPlaced.current = false;   // 每次重新布置(换素材/重开)都重新允许自动夹取
    setPos({ left, top, width });
  }, [anchor]);

  // 阶段二 — 按真实内容高度精修 top: 弹窗底若超出视口(内容超高)就整体上移, 根治"显示不全".
  // ResizeObserver: 打开即量一次; 之后内容长高(如分析/镜头结果回填)也会自动再夹一次.
  // 用户手动拖过(autoPlaced)就停手, 位置完全交给用户.
  useLayoutEffect(() => {
    if (!pos) return;
    const el = popRef.current;
    if (!el) return;
    const fit = () => {
      if (autoPlaced.current) return;
      const vh = window.innerHeight;
      const h = el.offsetHeight;
      const maxTop = vh - GAP - h;
      setPos(p => {
        if (!p) return p;
        const top = Math.max(GAP, Math.min(p.top, maxTop));
        return Math.abs(top - p.top) > 0.5 ? { ...p, top } : p;
      });
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [pos]);

  // ===== 拖动 (标题栏 pointer capture) =====
  const startDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    const el = popRef.current;
    if (!el || !pos) return;
    e.preventDefault();
    // 捕获必须放在持有 onPointerMove 的标题栏上: 捕获后后续事件全部派发给它, 指针移出标题/窗口也能继续拖.
    e.currentTarget.setPointerCapture(e.pointerId);
    autoPlaced.current = true;    // 一拖即放弃自动定位
    const r = el.getBoundingClientRect();
    dragRef.current = { x: e.clientX, y: e.clientY, left: r.left, top: r.top, width: r.width, height: r.height };
  };

  const onDragMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const st = dragRef.current;
    if (!st) return;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const rawLeft = st.left + (e.clientX - st.x);
    const rawTop = st.top + (e.clientY - st.y);
    // 全程夹取在视口内: 窗口再大也留边可抓, 不会拖丢或再次"显示不全"
    const left = Math.min(Math.max(rawLeft, GAP), Math.max(GAP, vw - GAP - st.width));
    const maxTop = vh - GAP - st.height;
    const top = Math.min(Math.max(rawTop, GAP), Math.max(GAP, maxTop));
    setPos(p => (p ? { ...p, left, top } : p));
  };

  const endDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* 已释放 */ }
  };

  // 捕获被系统夺走(如浏览器弹窗/异常)时清理拖动会话, 避免卡在拖态.
  const onLostPointerCapture = () => { dragRef.current = null; };

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
        {/* 标题栏 = 拖动柄: 按住任意处拖动整个弹窗; 自动夹取保证内容超高也不裁 */}
        <div
          onPointerDown={startDrag}
          onPointerMove={onDragMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          onLostPointerCapture={onLostPointerCapture}
          className="flex items-center justify-between border-b border-[#121212]/10 px-5 py-3 bg-[#121212]/3 cursor-grab active:cursor-grabbing select-none touch-none"
          title="按住标题栏拖动位置 · Esc 关闭"
        >
          <div className="flex items-center min-w-0 flex-1">
            <GripVertical size={14} strokeWidth={1.5} className="opacity-30 mr-2 flex-shrink-0" aria-hidden />
            <div className="font-serif italic text-lg truncate" title={asset.name}>{asset.name}</div>
          </div>
          <button onClick={onClose} onPointerDown={(e) => e.stopPropagation()}
            className="opacity-50 hover:opacity-100 transition-opacity flex-shrink-0 ml-3" title="关闭 (Esc)">
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
                  {!vj.content && vFallback && (
                    <p className="font-light leading-relaxed whitespace-pre-wrap">🎬 {vFallback}</p>
                  )}
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
