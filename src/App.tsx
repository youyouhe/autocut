/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { useState } from 'react';
import { Video, MessageSquare, FileEdit, MonitorPlay, Radio } from 'lucide-react';
import AssetPanel from './components/AssetPanel';
import ChatPanel from './components/ChatPanel';
import DraftPanel from './components/DraftPanel';
import RenderPanel from './components/RenderPanel';
import ReceiveDialog from './components/ReceiveDialog';

export default function App() {
  const [activeTab, setActiveTab] = useState('assets');
  const [isReceiveDialogOpen, setIsReceiveDialogOpen] = useState(false);

  const tabs = [
    { id: 'assets', label: 'Assets', icon: Video },
    { id: 'chat', label: 'Chat', icon: MessageSquare },
    { id: 'drafts', label: 'Drafts', icon: FileEdit },
    { id: 'render', label: 'Tasks', icon: MonitorPlay },
  ];

  return (
    <div className="flex h-screen w-full bg-[#FDFCF8] text-[#121212] font-sans overflow-hidden">
      {/* Sidebar */}
      <aside className="w-64 border-r border-[#121212]/10 flex flex-col">
        <div className="p-8 border-b border-[#121212]/10">
          <h1 className="text-3xl font-bold tracking-tighter italic font-serif">Workbench</h1>
          <div className="text-[9px] uppercase tracking-[0.3em] font-medium opacity-50 mt-2">System / v.2.4</div>
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
        <div className="p-8 border-t border-[#121212]/10">
          <button
            onClick={() => setIsReceiveDialogOpen(true)}
            className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-[#121212] hover:bg-[#121212]/80 text-[#FDFCF8] transition-colors text-[10px] uppercase tracking-widest font-bold"
          >
            <Radio size={16} strokeWidth={2} />
            LocalSend
          </button>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 overflow-hidden relative">
        {activeTab === 'assets' && <AssetPanel />}
        {activeTab === 'chat' && <ChatPanel />}
        {activeTab === 'drafts' && <DraftPanel />}
        {activeTab === 'render' && <RenderPanel />}
      </main>

      {/* Receive Dialog Modal */}
      {isReceiveDialogOpen && (
        <ReceiveDialog onClose={() => setIsReceiveDialogOpen(false)} />
      )}
    </div>
  );
}
