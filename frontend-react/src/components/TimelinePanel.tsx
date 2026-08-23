/**
 * 草稿时间线查看 (只读多轨道视图)
 * 模拟 CapCut 编辑器布局: 时间标尺 + 各轨道(video/audio/text/sticker…)的 segment 块按时间轴铺开.
 * 数据源: api.queryScript (POST /query_script → JSON.parse output).
 * 只读: 不做拖拽/增删, 仅展示草稿素材的组织关系.
 */
import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { Film, ArrowLeft, Loader2, Video, Music, Type, Sticker, Sparkles, Plus, Minus, Layers } from 'lucide-react';
import * as api from '../api';
import type { DraftContent, TimelineTrack, TimelineSegment } from '../api';

interface Props {
  draftId: string | null;
  setDraftId: (id: string | null) => void;
  onBack: () => void;
}

const LABEL_W = 140;            // 左侧轨道标签列宽
const TRACK_H = 56;             // 单条轨道行高
const MIN_PXPS = 10, MAX_PXPS = 120, DEF_PXPS = 40;

// 轨道类型排序与图标 (render 顺序: video→audio→text→sticker→effect/filter)
const TRACK_ORDER: Record<string, number> = { video: 0, audio: 1, text: 2, sticker: 3, effect: 4, filter: 5 };
function trackIcon(t: string) {
  switch (t) {
    case 'video': return Video;
    case 'audio': return Music;
    case 'text': return Type;
    case 'sticker': return Sticker;
    default: return Sparkles;
  }
}
function trackColor(t: string): string {
  switch (t) {
    case 'video': return 'bg-[#121212]';
    case 'audio': return 'bg-[#3b3b3b]';
    case 'text': return 'bg-[#FDFCF8] border border-[#121212]/40';
    case 'sticker': return 'bg-[#e8e2d4] border border-[#121212]/30';
    default: return 'bg-[#d4cfc2] border border-[#121212]/20';
  }
}

// 剪映文本素材的 content 是 {"styles":[...],"text":"实际文字"} 的 JSON; 退化用 mat.text.
// 解析失败/非 JSON 时回退原文 (防止把整段 JSON 涂到块上).
function extractText(content: unknown, fallback?: string): string {
  if (typeof content !== 'string' || !content) return fallback ?? '';
  if (!content.startsWith('{')) return content;
  try {
    const j = JSON.parse(content);
    if (j && typeof j.text === 'string') return j.text;
  } catch { /* fall through */ }
  return fallback ?? content;
}

export default function TimelinePanel({ draftId, setDraftId, onBack }: Props) {
  const [content, setContent] = useState<DraftContent | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pxPerSec, setPxPerSec] = useState(DEF_PXPS);
  const [drafts, setDrafts] = useState<api.Draft[]>([]);
  const [hoverSeg, setHoverSeg] = useState<TimelineSegment | null>(null);
  // 视频 hover 预览: 悬停 video segment 时弹出放大的 <video>, 定位到 source_timerange.start 处静音播放
  const [preview, setPreview] = useState<{ seg: TimelineSegment; x: number; y: number } | null>(null);
  const previewTimer = useRef<number | null>(null);

  const load = useCallback(async (id: string | null) => {
    if (!id) { setContent(null); setError(null); return; }
    setLoading(true); setError(null);
    try {
      const c = await api.queryScript(id);
      setContent(c);
    } catch (e) {
      setContent(null);
      setError((e as Error).message || '加载草稿时间线失败');
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(draftId); }, [draftId, load]);

  // 草稿选择器列表 (用于无 draftId / 加载失败时切换)
  useEffect(() => {
    api.listDrafts().then(setDrafts).catch(() => {});
  }, []);

  // materials 按 id 建索引 (取 path/media_path/material_name/content)
  const matIndex = useMemo(() => {
    const m = content?.materials ?? {};
    const byId: Record<string, any> = {};
    for (const arr of [m.videos, m.audios, m.texts, m.stickers, m.effects, m.filters]) {
      (arr ?? []).forEach(x => { if (x?.id) byId[x.id] = x; });
    }
    return byId;
  }, [content]);

  // 轨道排序
  const sortedTracks = useMemo(() => {
    const tracks = (content?.tracks ?? []).slice();
    tracks.sort((a, b) => (TRACK_ORDER[a.type] ?? 99) - (TRACK_ORDER[b.type] ?? 99));
    return tracks;
  }, [content]);

  const durSec = content ? (content.duration || 0) / 1e6 : 0;
  const totalW = Math.max(durSec * pxPerSec, 200);

  const fmtSec = (us: number) => (us / 1e6).toFixed(1) + 's';

  const zoom = (d: number) => setPxPerSec(p => Math.max(MIN_PXPS, Math.min(MAX_PXPS, p + d)));

  // 视频 hover 预览: 延迟 350ms 弹出, 避免鼠标快速划过时频繁建 <video>. 离开立即清.
  const openPreview = useCallback((seg: TimelineSegment, ev: React.MouseEvent) => {
    if (previewTimer.current) window.clearTimeout(previewTimer.current);
    previewTimer.current = window.setTimeout(() => {
      setPreview({ seg, x: ev.clientX, y: ev.clientY });
    }, 350);
  }, []);
  const closePreview = useCallback(() => {
    if (previewTimer.current) { window.clearTimeout(previewTimer.current); previewTimer.current = null; }
    setPreview(null);
  }, []);
  // 跟随鼠标移动更新定位 (preview 打开后)
  const movePreview = useCallback((ev: React.MouseEvent) => {
    setPreview(p => p ? { ...p, x: ev.clientX, y: ev.clientY } : p);
  }, []);
  useEffect(() => () => { if (previewTimer.current) window.clearTimeout(previewTimer.current); }, []);

  const rulerTicks = useMemo(() => {
    const ticks: { x: number; s: number; major: boolean }[] = [];
    const step = pxPerSec >= 30 ? 1 : pxPerSec >= 15 ? 2 : 5;   // 秒
    const majorEvery = step * 5;
    for (let s = 0; s <= Math.ceil(durSec); s += step) {
      ticks.push({ x: s * pxPerSec, s, major: (s % majorEvery) === 0 });
    }
    return ticks;
  }, [durSec, pxPerSec]);

  return (
    <div className="h-full w-full flex flex-col">
      {/* 顶栏 */}
      <div className="flex items-center justify-between px-12 py-6 border-b border-[#121212]/10">
        <div className="flex items-center gap-4">
          <Film size={24} strokeWidth={1.5} />
          <div>
            <h2 className="text-3xl font-light italic font-serif leading-none">Timeline</h2>
            <div className="text-[9px] uppercase tracking-[0.3em] opacity-40 mt-1 font-mono">
              {draftId ? draftId.slice(0, 12) + '…' : '— none —'}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {/* 缩放 */}
          <div className="flex items-center border border-[#121212]/20">
            <button onClick={() => zoom(-10)} className="px-3 py-2 hover:bg-[#121212]/5 transition-colors" title="缩小">
              <Minus size={14} strokeWidth={1.5} />
            </button>
            <span className="px-3 text-[10px] uppercase tracking-widest font-bold opacity-60">{pxPerSec}px/s</span>
            <button onClick={() => zoom(10)} className="px-3 py-2 hover:bg-[#121212]/5 transition-colors" title="放大">
              <Plus size={14} strokeWidth={1.5} />
            </button>
          </div>
          <button onClick={onBack}
            className="flex items-center gap-2 px-4 py-2 border border-[#121212]/20 hover:bg-[#121212]/5 transition-colors text-[10px] uppercase tracking-widest font-bold">
            <ArrowLeft size={14} strokeWidth={1.5} /> Back
          </button>
        </div>
      </div>

      {/* 主体 */}
      <div className="flex-1 overflow-hidden relative">
        {!draftId ? (
          <DraftPicker drafts={drafts} onPick={(id) => setDraftId(id)} />
        ) : loading ? (
          <div className="h-full flex items-center justify-center">
            <Loader2 className="animate-spin text-[#121212]" size={28} strokeWidth={1.5} />
          </div>
        ) : error ? (
          <div className="h-full flex flex-col items-center justify-center text-[#121212]/50">
            <Film size={48} className="mb-4 opacity-20" strokeWidth={1} />
            <p className="font-serif italic text-2xl mb-2">Unable to load timeline</p>
            <p className="text-[10px] uppercase tracking-widest opacity-60 max-w-md text-center mb-6">{error}</p>
            <DraftPicker drafts={drafts} onPick={(id) => setDraftId(id)} compact />
          </div>
        ) : !content || sortedTracks.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-[#121212]/50">
            <Layers size={48} className="mb-4 opacity-20" strokeWidth={1} />
            <p className="font-serif italic text-2xl">No segments</p>
            <p className="text-[10px] uppercase tracking-widest mt-2">This draft has no tracks</p>
          </div>
        ) : (
          <div className="h-full overflow-x-auto overflow-y-auto">
            <div style={{ width: totalW + LABEL_W }}>
              {/* 时间标尺 */}
              <div className="sticky top-0 z-20 flex bg-[#FDFCF8] border-b border-[#121212]/10">
                <div className="sticky left-0 z-10 bg-[#FDFCF8] border-r border-[#121212]/10" style={{ width: LABEL_W, height: 28 }}>
                  <div className="h-full flex items-center px-4 text-[9px] uppercase tracking-[0.3em] opacity-40 font-bold">Tracks</div>
                </div>
                <div className="relative flex-1" style={{ height: 28 }}>
                  {rulerTicks.map((t, i) => (
                    <div key={i} className="absolute top-0 bottom-0 flex flex-col items-start" style={{ left: t.x }}>
                      <div className={`w-px ${t.major ? 'h-3 bg-[#121212]/30' : 'h-2 bg-[#121212]/15'}`} />
                      {t.major && <span className="text-[9px] opacity-50 mt-0.5 ml-1 font-mono">{t.s}s</span>}
                    </div>
                  ))}
                </div>
              </div>

              {/* 轨道行 */}
              {sortedTracks.map((tr) => (
                <TrackRow key={tr.id} track={tr} matIndex={matIndex} pxPerSec={pxPerSec}
                  fmtSec={fmtSec} onHover={setHoverSeg}
                  onPreviewOpen={openPreview} onPreviewMove={movePreview} onPreviewClose={closePreview} />
              ))}
            </div>
          </div>
        )}
      </div>

      {/* hover tooltip */}
      {hoverSeg && (
        <div className="absolute bottom-4 right-4 bg-[#121212] text-[#FDFCF8] px-4 py-3 text-[10px] font-mono leading-relaxed z-30 max-w-xs">
          <div className="uppercase tracking-widest opacity-60 mb-1">{hoverSeg.type} segment</div>
          {hoverSeg.material_name && <div>{hoverSeg.material_name}</div>}
          {hoverSeg.text_content && <div className="truncate opacity-80">“{hoverSeg.text_content}”</div>}
          {hoverSeg.target_timerange && <div>target: {fmtSec(hoverSeg.target_timerange.start)} → {fmtSec(hoverSeg.target_timerange.start + hoverSeg.target_timerange.duration)}</div>}
          {hoverSeg.source_timerange && <div>source: {fmtSec(hoverSeg.source_timerange.start)} → {fmtSec(hoverSeg.source_timerange.start + hoverSeg.source_timerange.duration)}</div>}
          {hoverSeg.speed != null && hoverSeg.speed !== 1 && <div>speed: {hoverSeg.speed}×</div>}
          {hoverSeg.volume != null && <div>vol: {hoverSeg.volume}</div>}
        </div>
      )}

      {/* 视频 hover 预览: 放大 <video> 定位到 source_timerange.start 处静音播放, 跟随光标, 边缘 clamp */}
      {preview && preview.seg.material_path && (
        <VideoPreviewPopup seg={preview.seg} x={preview.x} y={preview.y} fmtSec={fmtSec} />
      )}
    </div>
  );
}

/** 视频 hover 预览弹层: 跟随光标定位, 边缘 clamp, 静音自动播放并 seek 到素材片段起点 */
function VideoPreviewPopup({ seg, x, y, fmtSec }: {
  seg: TimelineSegment;
  x: number;
  y: number;
  fmtSec: (us: number) => string;
}) {
  const ref = useRef<HTMLVideoElement>(null);
  // source_timerange.start 是素材原始时间轴起点 (微秒); 预览应展示该片段在源中的画面.
  const startSec = (seg.source_timerange?.start ?? 0) / 1e6;

  // 弹层尺寸 (宽固定 320, 高按视频比例; 没有元信息时用 16:9 兜底)
  const W = 320;
  const H = 180;

  // 视口边缘 clamp: 弹层显示在光标右下, 溢出则翻到左/上
  const vw = typeof window !== 'undefined' ? window.innerWidth : 1920;
  const vh = typeof window !== 'undefined' ? window.innerHeight : 1080;
  const gap = 16;
  const left = x + gap + W > vw ? x - gap - W : x + gap;
  const top = y + gap + H > vh ? y - gap - H : y + gap;

  return (
    <div className="fixed z-40 pointer-events-none shadow-2xl border border-[#121212]/30 bg-[#121212]"
      style={{ left, top, width: W }}>
      <video
        ref={ref}
        src={api.serveUrl(seg.material_path!)}
        muted
        autoPlay
        playsInline
        loop
        preload="auto"
        className="block w-full"
        style={{ height: H, objectFit: 'contain', background: '#000' }}
        onLoadedMetadata={() => {
          const v = ref.current;
          if (v) {
            try { v.currentTime = Math.min(startSec, Math.max((v.duration || 0) - 0.1, 0)); } catch { /* ignore seek */ }
            v.play().catch(() => { /* 自动播放被拒, 静音通常允许 */ });
          }
        }}
      />
      <div className="px-3 py-2 flex items-center justify-between gap-2 text-[#FDFCF8]">
        <span className="text-[9px] uppercase tracking-widest font-bold truncate opacity-80">
          {seg.material_name || 'video'}
        </span>
        {seg.source_timerange && (
          <span className="text-[9px] font-mono opacity-60 flex-shrink-0">
            {fmtSec(seg.source_timerange.start)}+
          </span>
        )}
      </div>
    </div>
  );
}

/** 单条轨道行: 左标签 + segment 块绝对定位 */
function TrackRow({ track, matIndex, pxPerSec, fmtSec, onHover, onPreviewOpen, onPreviewMove, onPreviewClose }: {
  track: TimelineTrack;
  matIndex: Record<string, any>;
  pxPerSec: number;
  fmtSec: (us: number) => string;
  onHover: (s: TimelineSegment | null) => void;
  onPreviewOpen: (seg: TimelineSegment, ev: React.MouseEvent) => void;
  onPreviewMove: (ev: React.MouseEvent) => void;
  onPreviewClose: () => void;
}) {
  const Icon = trackIcon(track.type);
  const segs = track.segments ?? [];
  return (
    <div className="flex border-b border-[#121212]/5">
      {/* 标签列 */}
      <div className="sticky left-0 z-10 bg-[#FDFCF8] border-r border-[#121212]/10 flex items-center gap-2 px-4"
        style={{ width: LABEL_W, height: TRACK_H }}>
        <Icon size={14} strokeWidth={1.5} className="opacity-60 flex-shrink-0" />
        <span className="text-[10px] uppercase tracking-widest font-bold truncate opacity-70">{track.type}</span>
      </div>
      {/* 轨道画布 */}
      <div className="relative flex-1" style={{ height: TRACK_H }}>
        {segs.map((seg) => {
          const tr = seg.target_timerange;
          if (!tr) return null;
          const left = (tr.start / 1e6) * pxPerSec;
          const width = Math.max((tr.duration / 1e6) * pxPerSec, 4);
          const mat = matIndex[seg.material_id] || {};
          // 剪映 video/audio 素材: 磁盘 draft 里 path 常为空, 真实路径在 media_path (绝对路径, 可直接 serve)
          const path = mat.path || mat.media_path;
          // 文本素材: content 是 {"styles":[...],"text":"实际文字"} 的 JSON, 退化取 mat.text
          const text = extractText(mat.content, mat.text);
          const segFull: TimelineSegment = {
            ...seg,
            type: track.type,
            material_path: path,
            material_name: mat.material_name || mat.name,
            text_content: text,
          };
          const isVideo = track.type === 'video';
          return (
            <div key={seg.id}
              onMouseEnter={(ev) => { onHover(segFull); if (isVideo && path) onPreviewOpen(segFull, ev); }}
              onMouseMove={(ev) => { if (isVideo && path) onPreviewMove(ev); }}
              onMouseLeave={() => { onHover(null); if (isVideo && path) onPreviewClose(); }}
              className={`absolute top-1.5 rounded-sm overflow-hidden ${trackColor(track.type)} ${isVideo && path ? 'cursor-pointer' : 'cursor-default'}`}
              style={{ left, width, height: TRACK_H - 12 }}
              title={`${track.type}`}>
              {track.type === 'video' && path ? (
                <>
                  <img src={api.serveUrl(path)} alt="" className="absolute inset-0 w-full h-full object-cover opacity-80" />
                  <div className="absolute inset-0 bg-black/30" />
                  <span className="absolute bottom-0.5 left-1 text-[9px] text-white font-mono">{fmtSec(tr.duration)}</span>
                </>
              ) : track.type === 'video' ? (
                // path 缺失: 纯色块 + 文件名, 不留空白让人误以为空轨道
                <span className="absolute inset-0 flex items-center px-2 text-[9px] text-[#FDFCF8]/80 font-mono truncate">
                  {mat.material_name || mat.name || 'video'}
                </span>
              ) : track.type === 'audio' ? (
                <div className="h-full flex items-center gap-px px-1">
                  {Array.from({ length: Math.max(Math.floor(width / 4), 3) }).map((_, i) => (
                    <div key={i} className="flex-1 bg-[#FDFCF8]/40"
                      style={{ height: `${30 + ((i * 37) % 50)}%`, alignSelf: 'center' }} />
                  ))}
                </div>
              ) : track.type === 'text' ? (
                <span className="absolute inset-0 flex items-center px-2 text-[10px] text-[#121212] truncate font-medium">
                  {text || 'text'}
                </span>
              ) : (
                <span className="absolute inset-0 flex items-center px-2 text-[9px] uppercase tracking-widest text-[#121212]/60 font-bold truncate">
                  {track.type}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** 草稿选择器 (无 draftId / 加载失败时) */
function DraftPicker({ drafts, onPick, compact }: { drafts: api.Draft[]; onPick: (id: string) => void; compact?: boolean }) {
  if (drafts.length === 0) {
    return (
      <div className={`flex flex-col items-center justify-center text-[#121212]/50 ${compact ? '' : 'h-full'}`}>
        <p className="font-serif italic text-xl">No drafts available</p>
        <p className="text-[10px] uppercase tracking-widest mt-2">Create a draft first</p>
      </div>
    );
  }
  return (
    <div className={`flex flex-col items-center justify-center text-[#121212]/50 ${compact ? '' : 'h-full'}`}>
      {!compact && <Film size={48} className="mb-4 opacity-20" strokeWidth={1} />}
      {!compact && <p className="font-serif italic text-2xl mb-6">Select a draft</p>}
      <div className="w-full max-w-md space-y-2 px-4">
        {drafts.map(d => (
          <button key={d.id} onClick={() => onPick(d.id)}
            className="w-full flex items-center justify-between px-4 py-3 border border-[#121212]/20 hover:border-[#121212]/50 hover:bg-[#121212]/5 transition-colors text-left">
            <div className="min-w-0">
              <div className="text-xs font-medium truncate text-[#121212]">{d.name}</div>
              <div className="text-[9px] font-mono opacity-50 mt-0.5">{d.id.slice(0, 12)}… · {d.duration.toFixed(1)}s</div>
            </div>
            <ArrowLeft size={14} strokeWidth={1.5} className="opacity-40 rotate-180 flex-shrink-0" />
          </button>
        ))}
      </div>
    </div>
  );
}
