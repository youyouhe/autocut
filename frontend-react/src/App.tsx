/**
 * AI 视频工作台 — 主布局
 * 极简编辑器风: 奶白底 + 黑, Cormorant Garamond 衬线大标题, 细描边卡片, 大写微字标签.
 * sidebar 4 tab + LocalSend; 共享状态 (assets / draftId) 提升到此, props 下发各面板.
 */
import { useState, useEffect, useCallback } from 'react';
import { Video, MessageSquare, FileEdit, MonitorPlay, Radio, LayoutTemplate, Settings } from 'lucide-react';
import AssetPanel from './components/AssetPanel';
import ChatPanel from './components/ChatPanel';
import DraftPanel from './components/DraftPanel';
import RenderPanel from './components/RenderPanel';
import TemplatesPanel from './components/TemplatesPanel';
import SettingsPanel from './components/SettingsPanel';
import ReceiveDialog from './components/ReceiveDialog';
import * as api from './api';
import type { Asset } from './api';

export default function App() {
  const [activeTab, setActiveTab] = useState('assets');
  const [isReceiveOpen, setIsReceiveOpen] = useState(false);

  // 共享状态
  const [assets, setAssets] = useState<Asset[]>([]);
  const [draftId, setDraftId] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [online, setOnline] = useState(true);

  const refreshAssets = useCallback(async () => {
    try {
      const list = await api.getAssets();
      // 保留已有 analysis/_duration/_cached (合并), 后端只回 name/path/type
      setAssets(prev => list.map(a => {
        const old = prev.find(p => p.path === a.path);
        return old ? { ...old, ...a } : a;
      }));
    } catch (e) { console.error(e); }
  }, []);

  const refreshAssetsWithCache = useCallback(async () => {
    // 上传/LocalSend 后: 拉列表 + 对新视频查缓存分析
    const list = await api.getAssets();
    setAssets(prev => {
      const merged = list.map(a => {
        const old = prev.find(p => p.path === a.path);
        return old ? { ...old, ...a } : a;
      });
      // 异步查缓存 (不阻塞渲染)
      merged.filter(a => a.type === 'video' && !a.analysis).forEach(async (a) => {
        try {
          const c = await api.checkCached(a.path);
          if (c.cached && c.result) {
            setAssets(cur => cur.map(x => x.path === a.path ? { ...x, analysis: c.result!, _cached: true } : x));
          }
        } catch { /* ignore */ }
      });
      return merged;
    });
  }, []);

  useEffect(() => { refreshAssetsWithCache(); }, [refreshAssetsWithCache]);

  // health 轮询
  useEffect(() => {
    let t: any;
    const check = async () => { try { await api.health(); setOnline(true); } catch { setOnline(false); } };
    check();
    t = setInterval(check, 10000);
    return () => clearInterval(t);
  }, []);

  const tabs = [
    { id: 'assets', label: 'Assets', icon: Video },
    { id: 'chat', label: 'Chat', icon: MessageSquare },
    { id: 'templates', label: 'Templates', icon: LayoutTemplate },
    { id: 'drafts', label: 'Drafts', icon: FileEdit },
    { id: 'render', label: 'Tasks', icon: MonitorPlay },
    { id: 'settings', label: 'Settings', icon: Settings },
  ];

  return (
    <div className="flex h-screen w-full bg-[#FDFCF8] text-[#121212] font-sans overflow-hidden">
      {/* Sidebar */}
      <aside className="w-64 border-r border-[#121212]/10 flex flex-col">
        <div className="p-8 border-b border-[#121212]/10">
          <h1 className="text-3xl font-bold tracking-tighter italic font-serif">Workbench</h1>
          <div className="flex items-center gap-2 mt-2">
            <span className="text-[9px] uppercase tracking-[0.3em] font-medium opacity-50">System / v2.4</span>
            <span className={`w-1.5 h-1.5 rounded-full ${online ? 'bg-emerald-600' : 'bg-red-600'}`} />
          </div>
        </div>
        <nav className="flex-1 p-8 space-y-4">
          {tabs.map(tab => {
            const Icon = tab.icon;
            const isActive = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`w-full flex items-center gap-4 transition-colors group ${
                  isActive ? 'text-[#121212]' : 'text-[#121212]/50 hover:text-[#121212]'
                }`}
              >
                <div className={`p-2 border ${isActive ? 'border-[#121212] bg-[#121212] text-white' : 'border-[#121212]/20 group-hover:border-[#121212]/50'}`}>
                  <Icon size={16} strokeWidth={1.5} />
                </div>
                <span className="text-[10px] uppercase tracking-widest font-bold">{tab.label}</span>
              </button>
            );
          })}
        </nav>

        {/* 当前草稿上下文 */}
        <div className="px-8 py-4 border-t border-[#121212]/10">
          <div className="text-[9px] uppercase tracking-[0.3em] opacity-40 mb-1">Draft Context</div>
          <div className="text-xs font-mono truncate" title={draftId ?? ''}>
            {draftId ? draftId.slice(0, 12) + '…' : '— none —'}
          </div>
        </div>

        <div className="p-8 border-t border-[#121212]/10">
          <button
            onClick={() => setIsReceiveOpen(true)}
            className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-[#121212] hover:bg-[#121212]/80 text-[#FDFCF8] transition-colors text-[10px] uppercase tracking-widest font-bold"
          >
            <Radio size={16} strokeWidth={2} />
            LocalSend
          </button>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 overflow-hidden relative">
        {activeTab === 'assets' && <AssetPanel assets={assets} setAssets={setAssets} refreshAssets={refreshAssetsWithCache} />}
        {activeTab === 'chat' && <ChatPanel assets={assets} draftId={draftId} setDraftId={setDraftId} conversationId={conversationId} setConversationId={setConversationId} refreshAssets={refreshAssets} />}
        {activeTab === 'templates' && <TemplatesPanel setDraftId={setDraftId} onGenerated={() => setActiveTab('drafts')} />}
        {activeTab === 'drafts' && <DraftPanel onRendered={() => setActiveTab('render')} onCreated={() => setActiveTab('chat')} setDraftId={setDraftId} />}
        {activeTab === 'render' && <RenderPanel />}
        {activeTab === 'settings' && <SettingsPanel />}
      </main>

      {isReceiveOpen && (
        <ReceiveDialog
          onClose={() => setIsReceiveOpen(false)}
          onReceived={refreshAssetsWithCache}
        />
      )}
    </div>
  );
}
