import { useState, useEffect } from 'react';
import { Clock, Calendar, Trash2, Video, RefreshCw, Loader2, Plus, MessageSquare, Film } from 'lucide-react';
import * as api from '../api';
import type { Draft } from '../api';

interface Props {
  onRendered: () => void;       // 提交渲染后切到 Tasks tab
  onCreated: () => void;        // 新建草稿后切到 Chat tab
  onOpenChat: (id: string) => void;  // 从某草稿进到它的对话列表 (setDraftId + 切 Chat tab)
  onOpenTimeline: (id: string) => void;  // 查看该草稿的多轨道时间线 (setDraftId + 切 Timeline tab)
  setDraftId: (id: string | null) => void;
}

export default function DraftPanel({ onRendered, onCreated, onOpenChat, onOpenTimeline, setDraftId }: Props) {
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [loading, setLoading] = useState(false);
  const [rendering, setRendering] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const fetchDrafts = async () => {
    setLoading(true);
    try { setDrafts(await api.listDrafts()); }
    catch (err) { console.error(err); }
    finally { setLoading(false); }
  };

  useEffect(() => { fetchDrafts(); }, []);

  const handleCreate = async () => {
    setCreating(true);
    try {
      const r = await api.createDraft();
      if (r.success && r.output?.draft_id) {
        setDraftId(r.output.draft_id);
        onCreated();
      } else {
        alert('新建草稿失败: ' + (r.error || '未知错误'));
      }
    } catch (err) {
      alert('新建草稿失败: ' + (err as Error).message);
    } finally { setCreating(false); }
  };

  const handleDelete = async (folder: string) => {
    if (!confirm('删除该草稿? 不可恢复.')) return;
    try {
      await api.deleteDraft(folder);
      setDrafts(d => d.filter(x => x.folder !== folder));
    } catch (err) {
      alert('删除草稿失败: ' + (err as Error).message);
    }
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
        <div className="flex gap-4">
          <button onClick={fetchDrafts}
            className="flex items-center gap-2 px-4 py-2 border border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5 transition-colors text-[10px] uppercase tracking-widest font-bold">
            <RefreshCw size={14} strokeWidth={1.5} /> Refresh
          </button>
          <button onClick={handleCreate} disabled={creating}
            className="flex items-center gap-2 px-4 py-2 bg-[#121212] hover:bg-[#121212]/80 text-white transition-colors disabled:opacity-50 text-[10px] uppercase tracking-widest font-bold">
            {creating ? <Loader2 size={14} strokeWidth={1.5} className="animate-spin" /> : <Plus size={14} strokeWidth={1.5} />}
            New Draft
          </button>
        </div>
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
              <div className="h-48 border-b border-[#121212]/10 relative overflow-hidden flex-shrink-0 bg-[#121212] flex items-center justify-center">
                {d.cover_url ? (
                  <img src={d.cover_url} alt={d.name} className="w-full h-full object-contain"
                    onError={(e) => { e.currentTarget.style.display = 'none'; }} />
                ) : (
                  <Video size={32} strokeWidth={1} className="text-[#FDFCF8]/20" />
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
                <div className="mt-6 pt-4 border-t border-[#121212]/10 flex justify-between items-center">
                  <div className="flex gap-4">
                    <button onClick={() => onOpenChat(d.id)}
                      className="flex items-center gap-2 text-[10px] uppercase tracking-widest font-bold opacity-60 hover:opacity-100 hover:text-[#121212] transition-colors"
                      title="Open this draft's conversations">
                      <MessageSquare size={12} strokeWidth={2} /> Chat
                    </button>
                    <button onClick={() => onOpenTimeline(d.id)}
                      className="flex items-center gap-2 text-[10px] uppercase tracking-widest font-bold opacity-60 hover:opacity-100 hover:text-[#121212] transition-colors"
                      title="View this draft's timeline">
                      <Film size={12} strokeWidth={2} /> Timeline
                    </button>
                  </div>
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
