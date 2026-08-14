import { useState, useEffect } from 'react';
import { Clock, Calendar, Trash2, Video, FileEdit, RefreshCw } from 'lucide-react';

type Draft = {
  folder: string;
  draft_id: string;
  draft_name: string;
  duration: number;
  cover_url: string;
  tm_draft_create: number;
  tm_draft_modified: number;
};

export default function DraftPanel() {
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [loading, setLoading] = useState(false);

  const fetchDrafts = async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/drafts');
      const data = await res.json();
      setDrafts(data.sort((a: Draft, b: Draft) => b.tm_draft_modified - a.tm_draft_modified));
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchDrafts();
  }, []);

  const handleDelete = async (folder: string) => {
    if (!confirm('Are you sure you want to delete this draft? This cannot be undone.')) return;
    
    try {
      await fetch(`/api/drafts/${folder}`, { method: 'DELETE' });
      fetchDrafts();
    } catch (err) {
      console.error(err);
    }
  };

  const formatTime = (ms: number) => {
    return new Date(ms).toLocaleString();
  };

  return (
    <div className="h-full w-full flex flex-col p-12 overflow-y-auto">
      <div className="flex items-center justify-between mb-12 border-b border-[#121212]/10 pb-6">
        <h2 className="text-4xl font-light italic font-serif">Project Drafts</h2>
        <button
          onClick={fetchDrafts}
          className="flex items-center gap-2 px-4 py-2 border border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5 transition-colors text-[10px] uppercase tracking-widest font-bold"
        >
          <RefreshCw size={14} strokeWidth={1.5} />
          Refresh
        </button>
      </div>

      {loading ? (
        <div className="flex-1 flex items-center justify-center">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-[#121212]"></div>
        </div>
      ) : drafts.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center text-[#121212]/50">
          <FileEdit size={48} className="mb-4 opacity-20" strokeWidth={1} />
          <p className="font-serif italic text-2xl">No drafts found</p>
          <p className="text-[10px] uppercase tracking-widest mt-2">Initialize via Chat Assistant</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-8">
          {drafts.map((draft) => (
            <div key={draft.draft_id} className="border border-[#121212]/10 bg-transparent overflow-hidden group flex flex-col hover:border-[#121212]/30 transition-colors">
              <div className="h-48 border-b border-[#121212]/10 relative overflow-hidden flex-shrink-0 bg-[#121212]/5">
                <img 
                  src={draft.cover_url} 
                  alt={draft.draft_name}
                  className="w-full h-full object-cover"
                  onError={(e) => {
                    e.currentTarget.style.display = 'none';
                    e.currentTarget.parentElement?.classList.add('flex', 'items-center', 'justify-center', 'bg-[#121212]', 'text-white/20');
                  }}
                />
                <div className="absolute bottom-3 right-3 bg-[#121212] text-[#FDFCF8] text-[9px] px-2 py-1 tracking-widest font-medium">
                  {draft.duration.toFixed(1)}S
                </div>
                <div className="absolute inset-0 bg-[#FDFCF8]/90 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center">
                  <button className="px-6 py-3 border border-[#121212] bg-[#121212] hover:bg-transparent text-[#FDFCF8] hover:text-[#121212] font-bold text-[10px] uppercase tracking-widest flex items-center gap-2 transition-colors">
                    <Video size={14} strokeWidth={2} />
                    Render
                  </button>
                </div>
              </div>
              <div className="p-6 flex-1 flex flex-col">
                <h3 className="font-serif text-xl italic line-clamp-2 mb-4" title={draft.draft_name}>
                  {draft.draft_name}
                </h3>
                <div className="mt-auto space-y-3">
                  <div className="flex items-center gap-3 text-[10px] uppercase tracking-widest opacity-60">
                    <Clock size={12} strokeWidth={1.5} />
                    <span>Edited {formatTime(draft.tm_draft_modified)}</span>
                  </div>
                  <div className="flex items-center gap-3 text-[10px] uppercase tracking-widest opacity-40">
                    <Calendar size={12} strokeWidth={1.5} />
                    <span>Created {formatTime(draft.tm_draft_create)}</span>
                  </div>
                </div>
                <div className="mt-6 pt-4 border-t border-[#121212]/10 flex justify-end">
                  <button 
                    onClick={() => handleDelete(draft.folder)}
                    className="flex items-center gap-2 text-[10px] uppercase tracking-widest font-bold opacity-40 hover:opacity-100 hover:text-red-700 transition-colors"
                    title="Delete Draft"
                  >
                    <Trash2 size={12} strokeWidth={2} />
                    Delete
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
