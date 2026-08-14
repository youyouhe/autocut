import { useState, useEffect, useRef } from 'react';
import { X, Radio, Loader2, PlaySquare, Square } from 'lucide-react';
import * as api from '../api';
import type { LocalSendStatus } from '../api';

interface Props {
  onClose: () => void;
  onReceived: () => Promise<void>;   // 关闭后刷新素材库
}

function CheckCircle2({ size, className, strokeWidth = 2 }: any) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"></path>
      <path d="m9 12 2 2 4-4"></path>
    </svg>
  );
}

export default function ReceiveDialog({ onClose, onReceived }: Props) {
  const [status, setStatus] = useState<LocalSendStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const pollRef = useRef<any>(null);

  const fetchStatus = async () => {
    try { setStatus(await api.localsendStatus()); }
    catch (err) { console.error(err); }
  };

  useEffect(() => {
    // 打开即启动接收端
    (async () => {
      setLoading(true);
      try {
        const r = await api.localsendStart();
        if (r && (r.running === false || r.error)) {
          setStartError(r.error || '接收端启动失败 (端口 53317 可能被占用, 请关闭官方 LocalSend)');
        }
      } catch (e: any) {
        setStartError(e.message);
      } finally { setLoading(false); }
      await fetchStatus();
      pollRef.current = setInterval(fetchStatus, 2000);
    })();
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleClose = async () => {
    if (pollRef.current) clearInterval(pollRef.current);
    const before = status?.received_count || 0;
    try { await api.localsendStop(); } catch { /* ignore */ }
    await onReceived();
    if (before > 0) {
      // 简单提示 (无 toast 库, 用 onClose 后由 App 刷新体现)
    }
    onClose();
  };

  const handleStart = async () => {
    setLoading(true); setStartError(null);
    try { await api.localsendStart(); } catch (e: any) { setStartError(e.message); }
    finally { setLoading(false); await fetchStatus(); }
  };

  const handleStop = async () => {
    setLoading(true);
    try { await api.localsendStop(); } catch { /* ignore */ }
    finally { setLoading(false); await fetchStatus(); }
  };

  const received = status?.received || [];

  return (
    <div className="fixed inset-0 bg-[#121212]/80 flex items-center justify-center z-50 p-4 backdrop-blur-sm">
      <div className="bg-[#FDFCF8] border border-[#121212]/20 shadow-2xl w-full max-w-lg overflow-hidden flex flex-col">
        <div className="p-6 border-b border-[#121212]/10 flex items-center justify-between">
          <h2 className="text-2xl font-light italic font-serif flex items-center gap-3 text-[#121212]">
            <Radio size={20} strokeWidth={1.5} className="opacity-70" /> LocalSend Receiver
          </h2>
          <button onClick={handleClose} className="p-2 text-[#121212]/40 hover:text-[#121212] transition-colors">
            <X size={20} strokeWidth={1.5} />
          </button>
        </div>

        <div className="p-8 flex-1 overflow-y-auto">
          {startError && (
            <div className="mb-6 text-xs text-red-700 border border-red-700/20 bg-red-50 p-3 font-mono">{startError}</div>
          )}
          {status ? (
            <div className="space-y-8">
              {/* 状态 + 启停 */}
              <div className="flex items-center justify-between p-6 border border-[#121212]/10">
                <div>
                  <div className="text-[9px] uppercase tracking-widest opacity-50 mb-2">System Status</div>
                  <div className="flex items-center gap-3">
                    <div className={`w-2 h-2 rounded-full ${status.running ? 'bg-emerald-600 animate-pulse' : 'bg-[#121212]/20'}`}></div>
                    <span className="font-serif italic text-xl text-[#121212]">{status.running ? 'Discoverable' : 'Offline'}</span>
                  </div>
                </div>
                <div>
                  {status.running ? (
                    <button onClick={handleStop} disabled={loading}
                      className="flex items-center gap-2 px-5 py-2.5 border border-[#121212]/20 hover:bg-[#121212]/5 text-[#121212] transition-colors text-[10px] uppercase tracking-widest font-bold">
                      {loading ? <Loader2 size={14} className="animate-spin" /> : <Square size={14} strokeWidth={2} />} Halt
                    </button>
                  ) : (
                    <button onClick={handleStart} disabled={loading}
                      className="flex items-center gap-2 px-5 py-2.5 border border-[#121212] bg-[#121212] hover:bg-transparent text-[#FDFCF8] hover:text-[#121212] transition-colors text-[10px] uppercase tracking-widest font-bold">
                      {loading ? <Loader2 size={14} className="animate-spin" /> : <PlaySquare size={14} strokeWidth={2} />} Initialize
                    </button>
                  )}
                </div>
              </div>

              {/* 设备信息 */}
              <div className="grid grid-cols-2 gap-0 border border-[#121212]/10">
                <div className="p-5 border-r border-[#121212]/10">
                  <div className="text-[9px] uppercase tracking-widest opacity-50 mb-2">Alias</div>
                  <div className="font-serif italic text-lg text-[#121212]">{status.alias || 'AI Workbench'}</div>
                </div>
                <div className="p-5">
                  <div className="text-[9px] uppercase tracking-widest opacity-50 mb-2">Network Vector</div>
                  <div className="text-[#121212] font-mono text-sm tracking-tight">{status.my_ip || '?'}:{status.port || 53317}</div>
                </div>
              </div>

              {/* 指引 */}
              {status.running && (
                <div className="text-[10px] uppercase tracking-widest opacity-50 leading-relaxed border-l-2 border-[#121212]/20 pl-4">
                  手机/电脑打开 LocalSend → 选「发送」→ 附近设备出现「{status.alias || 'AI Workbench'}」。<br />
                  搜不到时: 手动添加 {status.my_ip || '本机IP'}:53317
                </div>
              )}

              {/* 接收日志 */}
              {status.running && (
                <div>
                  <h3 className="text-[10px] font-bold text-[#121212] mb-4 uppercase tracking-widest border-b border-[#121212]/10 pb-2">
                    Ingestion Log ({status.received_count || 0})
                  </h3>
                  {received.length > 0 ? (
                    <ul className="space-y-0 border border-[#121212]/10">
                      {received.slice().reverse().map((file, i) => (
                        <li key={i} className={`flex justify-between items-center p-4 text-sm ${i !== received.length - 1 ? 'border-b border-[#121212]/10' : ''}`}>
                          <span className="truncate flex-1 font-serif italic text-lg text-[#121212] mr-4">{file.fileName}</span>
                          <span className="text-emerald-700 flex-shrink-0 flex items-center gap-2 text-[10px] uppercase tracking-widest font-bold">
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
