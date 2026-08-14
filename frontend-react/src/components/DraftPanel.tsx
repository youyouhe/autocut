import { useState, useEffect } from 'react';
import { Clock, Calendar, Trash2, Video, RefreshCw, Loader2 } from 'lucide-react';
import * as api from '../api';
import type { Draft } from '../api';

interface Props {
  onRendered: () => void;       // 提交渲染后切到 Tasks tab
  setDraftId: (id: string | null) => void;
}

export default function DraftPanel({ onRendered, setDraftId }: Props) {
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [loading, setLoading] = useState(false);
  const [rendering, setRendering] = useState<string | null>(null);

  const fetchDrafts = async () => {
    setLoading(true);
    try { setDrafts(await api.listDrafts()); }
    catch (err) { console.error(err); }
    finally { setLoading(false); }
  };

  useEffect(() => { fetchDrafts(); }, []);

  const handleDelete = async (folder: string) => {
    if (!confirm('删除该草稿? 不可恢复.')) return;
    try {
      await api.deleteDraft(folder);
      setDrafts(d => d.filter(x => x.folder !== folder));
    } catch (err) { console.error(err); }
  };

  const handleRender = async (d: Draft) => {
    setRendering(d.id);
    setDraftId(d.id);
    try {
      await api.renderDraft(d.id);
      onRendered();
    } catch (err) {
      // 失败回退: 用文件夹名再试 (draft_id UUID 与文件夹名不一致时)
      try {
        await api.renderDraft(d.folder);
        onRendered();
      } catch (err2) {
        alert('渲染失败: ' + (err2 as Error).message);
      }
    } finally { setRendering(null); }
  };

  const formatTime = (s: number) => s ? new Date(s * 1000).toLocaleString() : '';

  return (
    <div className="h-full w-full flex flex-col p-12 overflow-y-auto">
      <div className="flex items-center justify-between mb-12 border-b border-[#121212]/10 pb-6">
        <h2 className="text-4xl font-light italic font-serif">Project Drafts</h2>
        <button onClick={fetchDrafts}
          className="flex items-center gap-2 px-4 py-2 border border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5 transition-colors text-[10px] uppercase tracking-widest font-bold">
          <RefreshCw size={14} strokeWidth={1.5} /> Refresh
        </button>
      </div>

      {loading ? (
        <div className="flex-1 flex items-center justify-center">
          <Loader2 className="animate-spin text-[#121212]" size={28} strokeWidth={1.5} />
        </div>
      ) : drafts.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center text-[#121212]/50">
          <Video size={48} className="mb-4 opacity-20" strokeWidth={1} />
          <p className="font-serif italic text-2xl">No drafts found</p>
          <p className="text-[10px] uppercase tracking-widest mt-2">Create via Chat Assistant</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-8">
          {drafts.map((d) => (
            <div key={d.id} className="border border-[#121212]/10 bg-transparent overflow-hidden group flex flex-col hover:border-[#121212]/30 transition-colors">
              <div className="h-48 border-b border-[#121212]/10 relative overflow-hidden flex-shrink-0 bg-[#121212]/5 flex items-center justify-center">
                {d.cover_url ? (
                  <img src={d.cover_url} alt={d.name} className="w-full h-full object-cover"
                    onError={(e) => { e.currentTarget.style.display = 'none'; }} />
                ) : (
                  <Video size={32} strokeWidth={1} className="text-[#121212]/20" />
                )}
                <div className="absolute bottom-3 right-3 bg-[#121212] text-[#FDFCF8] text-[9px] px-2 py-1 tracking-widest font-medium">
                  {d.duration.toFixed(1)}S
                </div>
                <div className="absolute inset-0 bg-[#FDFCF8]/90 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center">
                  <button onClick={() => handleRender(d)} disabled={rendering === d.id}
                    className="px-6 py-3 border border-[#121212] bg-[#121212] hover:bg-transparent text-[#FDFCF8] hover:text-[#121212] font-bold text-[10px] uppercase tracking-widest flex items-center gap-2 transition-colors disabled:opacity-50">
                    {rendering === d.id ? <Loader2 size={14} strokeWidth={2} className="animate-spin" /> : <Video size={14} strokeWidth={2} />}
                    Render
                  </button>
                </div>
              </div>
              <div className="p-6 flex-1 flex flex-col">
                <h3 className="font-serif text-xl italic line-clamp-2 mb-4" title={d.name}>{d.name}</h3>
                <div className="mt-auto space-y-3">
                  <div className="flex items-center gap-3 text-[10px] uppercase tracking-widest opacity-60">
                    <Clock size={12} strokeWidth={1.5} /> <span>Edited {formatTime(d.modified)}</span>
                  </div>
                  <div className="flex items-center gap-3 text-[10px] uppercase tracking-widest opacity-40">
                    <Calendar size={12} strokeWidth={1.5} /> <span>Created {formatTime(d.created)}</span>
                  </div>
                </div>
                <div className="mt-6 pt-4 border-t border-[#121212]/10 flex justify-end">
                  <button onClick={() => handleDelete(d.folder)}
                    className="flex items-center gap-2 text-[10px] uppercase tracking-widest font-bold opacity-40 hover:opacity-100 hover:text-red-700 transition-colors"
                    title="Delete Draft">
                    <Trash2 size={12} strokeWidth={2} /> Delete
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
