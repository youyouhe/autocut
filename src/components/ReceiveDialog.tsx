import { useState, useEffect } from 'react';
import { X, Radio, Loader2, PlaySquare, Square } from 'lucide-react';

export default function ReceiveDialog({ onClose }: { onClose: () => void }) {
  const [status, setStatus] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  const fetchStatus = async () => {
    try {
      const res = await fetch('/api/localsend/status');
      const data = await res.json();
      setStatus(data);
    } catch (err) {
      console.error(err);
    }
  };

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 3000);
    return () => clearInterval(interval);
  }, []);

  const handleStart = async () => {
    setLoading(true);
    try {
      await fetch('/api/localsend/start', { method: 'POST' });
      await fetchStatus();
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const handleStop = async () => {
    setLoading(true);
    try {
      await fetch('/api/localsend/stop', { method: 'POST' });
      await fetchStatus();
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-[#121212]/80 flex items-center justify-center z-50 p-4 backdrop-blur-sm">
      <div className="bg-[#FDFCF8] border border-[#121212]/20 shadow-2xl w-full max-w-lg overflow-hidden flex flex-col">
        <div className="p-6 border-b border-[#121212]/10 flex items-center justify-between bg-transparent">
          <h2 className="text-2xl font-light italic font-serif flex items-center gap-3 text-[#121212]">
            <Radio size={20} strokeWidth={1.5} className="opacity-70" />
            LocalSend Receiver
          </h2>
          <button onClick={onClose} className="p-2 text-[#121212]/40 hover:text-[#121212] transition-colors">
            <X size={20} strokeWidth={1.5} />
          </button>
        </div>
        
        <div className="p-8 flex-1 overflow-y-auto">
          {status ? (
            <div className="space-y-8">
              <div className="flex items-center justify-between p-6 border border-[#121212]/10 bg-transparent">
                <div>
                  <div className="text-[9px] uppercase tracking-widest opacity-50 mb-2">System Status</div>
                  <div className="flex items-center gap-3">
                    <div className={`w-2 h-2 rounded-full ${status.running ? 'bg-emerald-600 animate-pulse' : 'bg-[#121212]/20'}`}></div>
                    <span className="font-serif italic text-xl text-[#121212]">
                      {status.running ? 'Discoverable' : 'Offline'}
                    </span>
                  </div>
                </div>
                <div>
                  {status.running ? (
                    <button onClick={handleStop} disabled={loading} className="flex items-center gap-2 px-5 py-2.5 border border-[#121212]/20 hover:bg-[#121212]/5 text-[#121212] transition-colors text-[10px] uppercase tracking-widest font-bold">
                      {loading ? <Loader2 size={14} className="animate-spin" /> : <Square size={14} strokeWidth={2} />}
                      Halt
                    </button>
                  ) : (
                    <button onClick={handleStart} disabled={loading} className="flex items-center gap-2 px-5 py-2.5 border border-[#121212] bg-[#121212] hover:bg-transparent text-[#FDFCF8] hover:text-[#121212] transition-colors text-[10px] uppercase tracking-widest font-bold">
                      {loading ? <Loader2 size={14} className="animate-spin" /> : <PlaySquare size={14} strokeWidth={2} />}
                      Initialize
                    </button>
                  )}
                </div>
              </div>

              <div className="grid grid-cols-2 gap-0 border border-[#121212]/10">
                <div className="p-5 border-r border-[#121212]/10 bg-transparent">
                  <div className="text-[9px] uppercase tracking-widest opacity-50 mb-2">Alias</div>
                  <div className="font-serif italic text-lg text-[#121212]">{status.device_name || 'AI Workbench'}</div>
                </div>
                <div className="p-5 bg-transparent">
                  <div className="text-[9px] uppercase tracking-widest opacity-50 mb-2">Network Vector</div>
                  <div className="text-[#121212] font-mono text-sm tracking-tight">{status.local_ip}:{status.port}</div>
                </div>
              </div>

              {status.running && (
                <div>
                  <h3 className="text-[10px] font-bold text-[#121212] mb-4 uppercase tracking-widest border-b border-[#121212]/10 pb-2">Ingestion Log</h3>
                  {status.received_list?.length > 0 ? (
                    <ul className="space-y-0 border border-[#121212]/10">
                      {status.received_list.map((file: any, i: number) => (
                        <li key={i} className={`flex justify-between items-center p-4 text-sm bg-transparent ${i !== status.received_list.length - 1 ? 'border-b border-[#121212]/10' : ''}`}>
                          <span className="truncate flex-1 font-serif italic text-lg text-[#121212]">{file.file_name || file}</span>
                          <span className="text-emerald-700 flex-shrink-0 ml-4 flex items-center gap-2 text-[10px] uppercase tracking-widest font-bold">
                            <CheckCircle2 size={14} strokeWidth={2} /> Delivered
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <div className="text-center p-8 border border-dashed border-[#121212]/20 text-[#121212]/40 text-[10px] uppercase tracking-widest">
                      Awaiting payload on active vector...
                    </div>
                  )}
                </div>
              )}
            </div>
          ) : (
            <div className="flex justify-center p-12">
              <Loader2 className="animate-spin text-[#121212]" size={32} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function CheckCircle2({size, className, strokeWidth = 2}: any) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"></path>
      <path d="m9 12 2 2 4-4"></path>
    </svg>
  );
}
