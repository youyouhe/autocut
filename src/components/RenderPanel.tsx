import { useState, useEffect } from 'react';
import { Download, MonitorPlay, Loader2, CheckCircle2, AlertCircle, RefreshCw } from 'lucide-react';

type RenderTask = {
  task_id: string;
  status: 'queued' | 'rendering' | 'done' | 'error';
  draft_name?: string;
  mp4_name?: string;
  duration?: number;
  download_url?: string;
  message?: string;
};

export default function RenderPanel() {
  const [tasks, setTasks] = useState<RenderTask[]>([]);
  const [loading, setLoading] = useState(false);

  const fetchTasks = async () => {
    setLoading(true);
    try {
      const res = await fetch('/render/list');
      const data = await res.json();
      setTasks(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTasks();
    const interval = setInterval(() => {
      // Periodic polling
      fetchTasks();
    }, 5000);
    return () => clearInterval(interval);
  }, []);

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'done': return <CheckCircle2 className="text-emerald-700" size={20} strokeWidth={1.5} />;
      case 'error': return <AlertCircle className="text-red-700" size={20} strokeWidth={1.5} />;
      case 'rendering': return <Loader2 className="text-[#121212] animate-spin" size={20} strokeWidth={1.5} />;
      default: return <Clock className="text-[#121212]/40" size={20} />;
    }
  };
  
  const Clock = ({className, size}: any) => (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10"></circle>
      <polyline points="12 6 12 12 16 14"></polyline>
    </svg>
  );

  return (
    <div className="h-full w-full flex flex-col p-12 overflow-y-auto">
      <div className="flex items-center justify-between mb-12 border-b border-[#121212]/10 pb-6">
        <h2 className="text-4xl font-light italic font-serif">Render Pipeline</h2>
        <button
          onClick={fetchTasks}
          className="flex items-center gap-2 px-4 py-2 border border-[#121212]/20 text-[#121212] hover:bg-[#121212]/5 transition-colors text-[10px] uppercase tracking-widest font-bold"
        >
          <RefreshCw size={14} strokeWidth={1.5} />
          Refresh
        </button>
      </div>

      {loading && tasks.length === 0 ? (
        <div className="flex-1 flex items-center justify-center">
          <Loader2 className="animate-spin text-[#121212]" size={32} />
        </div>
      ) : tasks.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center text-[#121212]/50">
          <MonitorPlay size={48} className="mb-4 opacity-20" strokeWidth={1} />
          <p className="font-serif italic text-2xl">No active processes</p>
          <p className="text-[10px] uppercase tracking-widest mt-2">Submit draft to initiate render</p>
        </div>
      ) : (
        <div className="space-y-0 max-w-5xl mx-auto w-full border border-[#121212]/10">
          {tasks.map((task, idx) => (
            <div key={task.task_id} className={`p-6 flex items-center gap-8 bg-transparent ${idx !== tasks.length - 1 ? 'border-b border-[#121212]/10' : ''}`}>
              <div className="flex-shrink-0 opacity-80">
                {getStatusIcon(task.status)}
              </div>
              
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-4 mb-2">
                  <h3 className="font-serif italic text-xl truncate text-[#121212]">
                    {task.draft_name || task.task_id}
                  </h3>
                  <span className={`text-[9px] px-2 py-1 uppercase tracking-widest font-bold border ${
                    task.status === 'done' ? 'border-emerald-700/30 text-emerald-700 bg-emerald-50/50' :
                    task.status === 'error' ? 'border-red-700/30 text-red-700 bg-red-50/50' :
                    task.status === 'rendering' ? 'border-[#121212]/30 text-[#121212] bg-[#121212]/5' :
                    'border-[#121212]/10 text-[#121212]/60'
                  }`}>
                    {task.status}
                  </span>
                </div>
                
                <div className="text-[10px] uppercase tracking-widest opacity-50 flex gap-6">
                  <span>ID: {task.task_id}</span>
                  {task.duration && <span>Duration: {task.duration}s</span>}
                  {task.mp4_name && <span className="truncate max-w-[200px]">File: {task.mp4_name}</span>}
                </div>
                
                {task.status === 'error' && task.message && (
                  <div className="mt-3 text-xs text-red-700 border border-red-700/20 bg-red-50 p-3 font-mono">
                    {task.message}
                  </div>
                )}
              </div>

              {task.status === 'done' && task.download_url && (
                <a 
                  href={task.download_url}
                  download
                  className="flex-shrink-0 flex items-center gap-2 px-6 py-3 border border-[#121212] bg-[#121212] hover:bg-transparent text-[#FDFCF8] hover:text-[#121212] transition-colors text-[10px] uppercase tracking-widest font-bold"
                >
                  <Download size={14} strokeWidth={2} />
                  Download
                </a>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
