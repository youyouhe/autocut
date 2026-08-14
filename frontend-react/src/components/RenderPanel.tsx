import { useState, useEffect, useRef } from 'react';
import { Download, MonitorPlay, Loader2, CheckCircle2, AlertCircle, RefreshCw, Clock } from 'lucide-react';
import * as api from '../api';
import type { RenderTask } from '../api';

const ACTIVE: RenderTask['status'][] = ['queued', 'rendering'];

export default function RenderPanel() {
  const [tasks, setTasks] = useState<RenderTask[]>([]);
  const [loading, setLoading] = useState(true);
  const timerRef = useRef<any>(null);

  const fetchTasks = async () => {
    try { setTasks(await api.renderList()); }
    catch (err) { console.error(err); }
    finally { setLoading(false); }
  };

  const pollAll = async () => {
    setTasks(prev => {
      const active = prev.filter(t => ACTIVE.includes(t.status));
      if (active.length === 0) { stopPoll(); }
      return prev;
    });
    // 单独刷新 (避免闭包)
    const list = await api.renderList().catch(() => null);
    if (list) setTasks(list);
  };

  const startPoll = () => {
    if (timerRef.current) return;
    timerRef.current = setInterval(pollAll, 5000);
  };
  const stopPoll = () => {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
  };

  useEffect(() => {
    (async () => {
      await fetchTasks();
    })();
    return () => stopPoll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (tasks.some(t => ACTIVE.includes(t.status))) startPoll();
    else stopPoll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tasks]);

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'done': return <CheckCircle2 className="text-emerald-700" size={20} strokeWidth={1.5} />;
      case 'error': return <AlertCircle className="text-red-700" size={20} strokeWidth={1.5} />;
      case 'rendering': return <Loader2 className="text-[#121212] animate-spin" size={20} strokeWidth={1.5} />;
      default: return <Clock className="text-[#121212]/40" size={20} />;
    }
  };

  const isActive = tasks.some(t => ACTIVE.includes(t.status));

  return (
    <div className="h-full w-full flex flex-col p-12 overflow-y-auto">
      <div className="flex items-center justify-between mb-12 border-b border-[#121212]/10 pb-6">
        <h2 className="text-4xl font-light italic font-serif">Render Pipeline</h2>
        <button onClick={fetchTasks}
          className="flex items-center gap-2 px-4 py-2 border border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5 transition-colors text-[10px] uppercase tracking-widest font-bold">
          <RefreshCw size={14} strokeWidth={1.5} /> Refresh
        </button>
      </div>

      {isActive && (
        <div className="mb-6 flex items-center gap-2 text-[10px] uppercase tracking-widest font-bold text-[#121212]/60">
          <Loader2 size={12} className="animate-spin" /> Polling render status…
        </div>
      )}

      {loading ? (
        <div className="flex-1 flex items-center justify-center">
          <Loader2 className="animate-spin text-[#121212]" size={32} strokeWidth={1.5} />
        </div>
      ) : tasks.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center text-[#121212]/50">
          <MonitorPlay size={48} className="mb-4 opacity-20" strokeWidth={1} />
          <p className="font-serif italic text-2xl">No active processes</p>
          <p className="text-[10px] uppercase tracking-widest mt-2">Submit a draft to initiate render</p>
        </div>
      ) : (
        <div className="space-y-0 max-w-5xl mx-auto w-full border border-[#121212]/10">
          {tasks.map((task, idx) => (
            <div key={task.task_id} className={`p-6 flex items-center gap-8 bg-transparent ${idx !== tasks.length - 1 ? 'border-b border-[#121212]/10' : ''}`}>
              <div className="flex-shrink-0 opacity-80">{getStatusIcon(task.status)}</div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-4 mb-2">
                  <h3 className="font-serif italic text-xl truncate text-[#121212]">{task.draft || task.draft_name || task.task_id}</h3>
                  <span className={`text-[9px] px-2 py-1 uppercase tracking-widest font-bold border ${
                    task.status === 'done' ? 'border-emerald-700/30 text-emerald-700 bg-emerald-50/50' :
                    task.status === 'error' ? 'border-red-700/30 text-red-700 bg-red-50/50' :
                    task.status === 'rendering' ? 'border-[#121212]/30 text-[#121212] bg-[#121212]/5' :
                    'border-[#121212]/10 text-[#121212]/60'
                  }`}>{task.status}</span>
                </div>
                <div className="text-[10px] uppercase tracking-widest opacity-50 flex gap-6">
                  <span>ID: {task.task_id}</span>
                  {task.duration != null && <span>{task.duration.toFixed(0)}s</span>}
                  {(task.mp4 || task.mp4_name) && <span className="truncate max-w-[200px]">File: {task.mp4 || task.mp4_name}</span>}
                </div>
                {task.status === 'error' && task.error && (
                  <div className="mt-3 text-xs text-red-700 border border-red-700/20 bg-red-50 p-3 font-mono break-all">{task.error}</div>
                )}
              </div>
              {task.status === 'done' && (
                <a href={api.downloadUrl(task.task_id)} download
                  className="flex-shrink-0 flex items-center gap-2 px-6 py-3 border border-[#121212] bg-[#121212] hover:bg-transparent text-[#FDFCF8] hover:text-[#121212] transition-colors text-[10px] uppercase tracking-widest font-bold">
                  <Download size={14} strokeWidth={2} /> Download
                </a>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
