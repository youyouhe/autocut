import { useState, useRef, useEffect } from 'react';
import { Send, Bot, User, SquareTerminal, Plus, Trash2, Loader2, MessageSquare, ChevronDown, FileEdit } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import * as api from '../api';
import type { Asset, ChatChunk, ChatMessage, ConversationSummary, Draft } from '../api';

type Message = ChatMessage;

const GREETING: Message = { role: 'assistant', content: '你好,我是 AI 视频编辑助手。描述你想做的视频,我会用导入的素材组装草稿并渲染。' };

interface Props {
  assets: Asset[];
  draftId: string | null;
  setDraftId: (id: string | null) => void;
  conversationId: string | null;
  setConversationId: (id: string | null) => void;
  refreshAssets: () => Promise<void>;
}

export default function ChatPanel({ assets, draftId, setDraftId, conversationId, setConversationId, refreshAssets }: Props) {
  const [messages, setMessages] = useState<Message[]>([GREETING]);
  const [input, setInput] = useState('');
  const [isTyping, setIsTyping] = useState(false);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [loadingConv, setLoadingConv] = useState(false);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [draftMenuOpen, setDraftMenuOpen] = useState(false);
  const [creatingDraft, setCreatingDraft] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  // 内层内容盒: ResizeObserver 的观察对象. 容器自身尺寸不变(它是视口), 内容盒的高度
  // 才随消息/媒体增长 —— 视频预览晚拿到 metadata、markdown/代码块晚排版、新消息插入,
  // 全都会撑高内容盒, RO 都能捕获, 这是"状态驱动的 effect"覆盖不了的异步布局增长.
  const contentRef = useRef<HTMLDivElement>(null);
  // 用户是否"贴着底部" (距底 <80px). 流式输出时若用户主动上翻看历史, 不强制拉回底部,
  // 只有本来就贴底(或刚发新消息)才自动跟随 —— 标准聊天面板行为.
  const stickToBottom = useRef(true);
  const lastScrollTop = useRef(0);
  const didInit = useRef(false);

  const scrollToBottom = (smooth = false) => {
    const el = scrollRef.current;
    if (!el) return;
    // 直接滚消息容器本身. 不用 scrollIntoView: 它默认 block:'start' 会把"底部标记"顶端
    // 对齐到视口顶(等于把最新一条滚出视野), 且会连带滚动所有可滚的祖先容器.
    // 默认 instant: 流式补滚要立刻到位, smooth 动画到的是发起时的旧目标, 内容一长高
    // 就落在半路 (86K px 长页面 + smooth 尤其明显, 感知上"根本没滚").
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
  };
  // 内容盒长高就补滚(贴底状态下). 治"晚熟布局": 挂载时 scrollTo 用的是当时高度,
  // 之后的视频meta加载/排版落定把内容撑高了 1728px, 没有 RO 就永远卡在离底一截.
  useEffect(() => {
    const content = contentRef.current;
    if (!content || typeof ResizeObserver === 'undefined') return;
    let lastH = content.offsetHeight;
    const ro = new ResizeObserver(() => {
      const h = content.offsetHeight;
      const grew = h > lastH + 1;
      lastH = h;
      if (grew && stickToBottom.current) scrollToBottom(false);
    });
    ro.observe(content);
    return () => ro.disconnect();
  }, []);
  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    // 方向感知: 只有"scrollTop 明显变小"(用户真实上翻)才取消跟随.
    // 不能只看距底距离 —— 自动滚动动画进行中触发的事件距离底部 >80px,
    // 若据此取消贴底标记, 流式输出后续内容就永远不再跟随了 (上一版的竞态 bug).
    const goingUp = el.scrollTop < lastScrollTop.current - 4;
    lastScrollTop.current = el.scrollTop;
    if (goingUp) {
      stickToBottom.current = false;
    } else if (el.scrollHeight - el.scrollTop - el.clientHeight < 80) {
      stickToBottom.current = true;
    }
  };
  // 依赖"条数 + 最后一条内容长度 + typing 状态": 覆盖 新消息追加 / 流式文本增长 /
  // tool 卡片插入 / 加载指示出现消失. 只依赖数组身份会漏掉原地改内容的场景.
  // (异步布局增长由上面的 ResizeObserver 兜底, 这里的 effect 管状态变化触发的即时滚动)
  const lastMsg = messages[messages.length - 1];
  const lastLen = lastMsg ? lastMsg.content.length : 0;
  useEffect(() => {
    if (stickToBottom.current) scrollToBottom(false);
  }, [messages.length, lastLen, isTyping, loadingConv]);

  const fetchConversations = async (dId?: string | null) => {
    const target = dId ?? draftId;
    if (!target) { setConversations([]); return; }
    try { setConversations(await api.listConversations(target)); }
    catch (err) { console.error(err); }
  };

  const fetchDrafts = async () => {
    try { setDrafts(await api.listDrafts()); }
    catch (err) { console.error(err); }
  };

  // 选一个草稿作为当前草稿上下文: 刷新侧栏对话列表 + 自动加载最近一条对话
  const selectDraft = async (dId: string | null) => {
    setDraftId(dId);
    setDraftMenuOpen(false);
    setConversationId(null);
    setMessages([GREETING]);
    if (!dId) { setConversations([]); return; }
    const list = await api.listConversations(dId).catch(() => []);
    setConversations(list);
    if (list.length > 0) {
      await loadConversation(list[0].id);
    }
  };

  const loadConversation = async (id: string) => {
    setLoadingConv(true);
    try {
      const conv = await api.getConversation(id);
      setConversationId(conv.id);
      // 切换会话: 内容整体替换, 强制回到贴底状态 (新内容把 scrollTop 归零会误判成"上翻")
      stickToBottom.current = true;
      lastScrollTop.current = 0;
      setMessages(conv.messages.length ? conv.messages : [GREETING]);
      // 新模型下侧栏只列当前草稿的对话, conv.draft_id === draftId, 此行仅作兜底
      if (conv.draft_id && conv.draft_id !== draftId) setDraftId(conv.draft_id);
    } catch (err) {
      console.error(err);
    } finally { setLoadingConv(false); }
  };

  // 首次挂载: 确定当前草稿上下文, 再拉它的对话列表.
  // 有 draftId → 直接用; 无 → 自动选最近一个草稿; 都没有 → 空状态 (点 New Chat 会自动建草稿).
  useEffect(() => {
    if (didInit.current) return;
    didInit.current = true;
    (async () => {
      await fetchDrafts();
      if (draftId) {
        await selectDraft(draftId);
      } else {
        const list = await api.listDrafts().catch(() => []);
        if (list.length > 0) {
          await selectDraft(list[0].id);
        }
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleNewChat = async () => {
    // 每个对话必须关联一个草稿. 当前无草稿时自动建一个空草稿再绑定.
    let dId = draftId;
    if (!dId) {
      setCreatingDraft(true);
      try {
        const r = await api.createDraft();
        if (r.success && r.output?.draft_id) {
          dId = r.output.draft_id;
          setDraftId(dId);
          await fetchDrafts();
        } else {
          alert('新建草稿失败: ' + (r.error || '未知错误'));
          return;
        }
      } catch (err) {
        alert('新建草稿失败: ' + (err as Error).message);
        return;
      } finally { setCreatingDraft(false); }
    }
    setConversationId(null);
    setMessages([GREETING]);
  };

  const handleSelectConversation = (id: string) => {
    if (id === conversationId) return;
    loadConversation(id);
  };

  const handleDeleteConversation = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm('删除这条对话记录? 不可恢复.')) return;
    try {
      await api.deleteConversation(id);
      setConversations(prev => prev.filter(c => c.id !== id));
      if (id === conversationId) handleNewChat();
    } catch (err) { console.error(err); }
  };

  const handleSend = async () => {
    const userMsg = input.trim();
    if (!userMsg || isTyping) return;
    // 每个对话必须关联草稿; 没有就先建一个 (用户直接在空状态发消息的情况)
    let activeDraft = draftId;
    if (!activeDraft) {
      try {
        const r = await api.createDraft();
        if (r.success && r.output?.draft_id) {
          activeDraft = r.output.draft_id;
          setDraftId(activeDraft);
          await fetchDrafts();
        } else {
          alert('需要先有草稿才能对话, 但新建草稿失败: ' + (r.error || '未知错误'));
          return;
        }
      } catch (err) {
        alert('新建草稿失败: ' + (err as Error).message);
        return;
      }
    }
    setInput('');
    // 自己刚发出消息: 无条件跟随到底部 (即使之前上翻在看历史)
    stickToBottom.current = true;
    setMessages(prev => [...prev, { role: 'user', content: userMsg }]);
    setIsTyping(true);

    const assetPaths = assets.map(a => a.path);
    let assistantContent = '';
    setMessages(prev => [...prev, { role: 'assistant', content: '' }]);

    try {
      await api.chatStream(
        { message: userMsg, draft_id: activeDraft, asset_paths: assetPaths, conversation_id: conversationId },
        (c: ChatChunk) => {
          if (c.conversation_id && c.conversation_id !== conversationId) {
            setConversationId(c.conversation_id);
          }
          if (c.text) {
            assistantContent += c.text;
            setMessages(prev => {
              const n = [...prev]; n[n.length - 1].content = assistantContent; return n;
            });
          } else if (c.tool) {
            // 在当前 assistant 消息后插入 tool 卡片 + 续接空 assistant
            setMessages(prev => [
              ...prev.slice(0, -1),
              { role: 'tool', content: `Invoked: ${c.tool}`, toolDetails: { tool: c.tool!, args: c.args, result: c.result } },
              { role: 'assistant', content: assistantContent },
            ]);
          } else if (c.draft_id && c.draft_id !== activeDraft) {
            // agent 新建/切换了草稿: 对话从此归属新草稿, 同步上下文并刷新草稿列表+侧栏
            activeDraft = c.draft_id;
            setDraftId(activeDraft);
            fetchDrafts();
          }
        },
      );
    } catch (err: any) {
      setMessages(prev => {
        const n = [...prev]; n[n.length - 1].content = `❌ 对话失败: ${err.message}`; return n;
      });
    } finally {
      setIsTyping(false);
      // 对话可能产生草稿/素材变化, 刷新
      refreshAssets();
      fetchConversations(activeDraft);
    }
  };

  const formatTime = (s: number) => {
    const d = new Date(s * 1000);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return sameDay ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : d.toLocaleDateString();
  };

  /** 工具调用涉及哪个素材(按 name 或本地路径匹配 assets), 供内嵌预览用. 匹配不到(比如远程 URL)就不展示预览. */
  const findAssetForTool = (tool?: string, args?: any): Asset | null => {
    if (!tool || !args) return null;
    if (['analyze_resource', 'get_resource_detail', 'get_transcript'].includes(tool) && args.name) {
      return assets.find(a => a.name === args.name) || null;
    }
    if (['add_video', 'add_audio', 'add_image'].includes(tool) && args.url) {
      return assets.find(a => a.path === args.url) || null;
    }
    return null;
  };

  const MediaPreview = ({ tool, args }: { tool?: string; args?: any }) => {
    const asset = findAssetForTool(tool, args);
    if (!asset) return null;
    if (asset.type === 'video') {
      return <video controls className="w-full max-h-64 mb-3 bg-black" src={api.serveUrl(asset.path)} />;
    }
    if (asset.type === 'audio') {
      return <audio controls className="w-full mb-3" src={api.serveUrl(asset.path)} />;
    }
    if (asset.type === 'image') {
      return <img className="max-h-64 mb-3 object-contain" src={api.serveUrl(asset.path)} alt={asset.name} />;
    }
    return null;
  };

  return (
    <div className="h-full w-full flex bg-transparent">
      {/* 历史会话侧栏 */}
      <div className="w-64 border-r border-[#121212]/10 flex flex-col flex-shrink-0">
        {/* 草稿切换器: 侧栏只列当前草稿的对话, 换草稿换一组 */}
        <div className="p-4 border-b border-[#121212]/10 relative">
          <div className="text-[9px] uppercase tracking-[0.3em] opacity-40 mb-2">Draft Context</div>
          <button onClick={() => setDraftMenuOpen(o => !o)}
            className="w-full flex items-center justify-between gap-2 px-3 py-2.5 border border-[#121212]/20 hover:border-[#121212] transition-colors text-left">
            <div className="flex items-center gap-2 min-w-0">
              <FileEdit size={13} strokeWidth={1.5} className="opacity-60 flex-shrink-0" />
              <span className="text-xs font-mono truncate">
                {draftId ? draftId.slice(0, 8) + '…' : '— none —'}
              </span>
            </div>
            <ChevronDown size={13} strokeWidth={1.5} className="opacity-40 flex-shrink-0" />
          </button>
          {draftMenuOpen && (
            <>
              <div className="fixed inset-0 z-20" onClick={() => setDraftMenuOpen(false)} />
              <div className="absolute left-4 right-4 top-full mt-1 border border-[#121212]/20 bg-[#FDFCF8] z-30 max-h-72 overflow-y-auto shadow-lg">
                {drafts.length === 0 ? (
                  <div className="p-4 text-[10px] uppercase tracking-widest opacity-40 text-center">No drafts</div>
                ) : drafts.map(d => (
                  <div key={d.id} onClick={() => selectDraft(d.id)}
                    className={`px-3 py-2.5 cursor-pointer border-b border-[#121212]/5 last:border-0 flex items-center gap-2 hover:bg-[#121212]/5 transition-colors ${
                      d.id === draftId ? 'bg-[#121212]/5' : ''
                    }`}>
                    <FileEdit size={12} strokeWidth={1.5} className="opacity-50 flex-shrink-0" />
                    <div className="min-w-0 flex-1">
                      <div className="text-xs font-light truncate">{d.name || d.id.slice(0, 8)}</div>
                      <div className="text-[9px] uppercase tracking-widest opacity-40 font-mono">{d.id.slice(0, 12)}…</div>
                    </div>
                  </div>
                ))}
                <button onClick={() => { setDraftMenuOpen(false); handleNewChat(); }}
                  className="w-full flex items-center justify-center gap-2 px-3 py-2.5 border-t border-[#121212]/10 bg-[#121212] hover:bg-[#121212]/80 text-[#FDFCF8] transition-colors text-[10px] uppercase tracking-widest font-bold">
                  <Plus size={12} strokeWidth={2} /> New Draft + Chat
                </button>
              </div>
            </>
          )}
        </div>
        <div className="p-4 border-b border-[#121212]/10">
          <button onClick={handleNewChat} disabled={creatingDraft}
            className="w-full flex items-center justify-center gap-2 px-4 py-2.5 border border-[#121212] bg-[#121212] hover:bg-transparent text-[#FDFCF8] hover:text-[#121212] transition-colors text-[10px] uppercase tracking-widest font-bold disabled:opacity-50">
            {creatingDraft ? <Loader2 size={14} strokeWidth={2} className="animate-spin" /> : <Plus size={14} strokeWidth={2} />}
            New Chat
          </button>
        </div>
        <div className="flex-1 overflow-y-auto">
          {!draftId ? (
            <div className="p-6 text-[10px] uppercase tracking-widest opacity-40 text-center">Select a draft above</div>
          ) : conversations.length === 0 ? (
            <div className="p-6 text-[10px] uppercase tracking-widest opacity-40 text-center">No chats in this draft</div>
          ) : (
            conversations.map(c => (
              <div key={c.id} onClick={() => handleSelectConversation(c.id)}
                className={`group px-5 py-4 border-b border-[#121212]/5 cursor-pointer transition-colors flex items-start gap-3 ${
                  c.id === conversationId ? 'bg-[#121212]/5' : 'hover:bg-[#121212]/5'
                }`}>
                <MessageSquare size={14} strokeWidth={1.5} className="opacity-40 flex-shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-light truncate text-[#121212]">{c.title || '(未命名对话)'}</div>
                  <div className="text-[9px] uppercase tracking-widest opacity-40 mt-1">{formatTime(c.updated_at)}</div>
                </div>
                <button onClick={(e) => handleDeleteConversation(c.id, e)}
                  className="opacity-0 group-hover:opacity-60 hover:!opacity-100 hover:text-red-700 transition-opacity flex-shrink-0" title="Delete">
                  <Trash2 size={13} strokeWidth={1.5} />
                </button>
              </div>
            ))
          )}
        </div>
      </div>

      {/* 主对话区 */}
      <div className="flex-1 flex flex-col min-w-0">
        <div className="p-8 border-b border-[#121212]/10 z-10 flex justify-between items-center">
          <h2 className="text-3xl font-light italic font-serif">AI Assistant</h2>
          <div className="text-[10px] uppercase tracking-widest font-bold border border-[#121212]/10 px-3 py-1 text-[#121212]">
            {draftId ? `Draft: ${draftId.slice(0, 8)}…` : 'No Draft'}
          </div>
        </div>

        <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto p-8">
          <div ref={contentRef} className="space-y-8">
          {loadingConv ? (
            <div className="flex justify-center p-12"><Loader2 className="animate-spin text-[#121212]" size={28} /></div>
          ) : (
            messages.map((msg, idx) => (
              <div key={idx} className={`flex gap-6 ${msg.role === 'user' ? 'flex-row-reverse' : ''}`}>
                <div className={`w-10 h-10 border flex items-center justify-center flex-shrink-0 ${
                  msg.role === 'user' ? 'border-[#121212] bg-[#121212] text-[#FDFCF8]' :
                  msg.role === 'tool' ? 'border-[#121212]/20 text-[#121212]/60' : 'border-[#121212]/20 text-[#121212]'
                }`}>
                  {msg.role === 'user' ? <User size={18} strokeWidth={1.5} /> :
                   msg.role === 'tool' ? <SquareTerminal size={18} strokeWidth={1.5} /> : <Bot size={18} strokeWidth={1.5} />}
                </div>
                <div className={`max-w-[75%] p-6 ${
                  msg.role === 'user' ? 'bg-[#121212] text-[#FDFCF8]' :
                  msg.role === 'tool' ? 'border border-[#121212]/10 bg-[#121212]/5 text-[#121212] text-xs font-mono' :
                  'border border-[#121212]/10 text-[#121212] bg-white/50'
                }`}>
                  {msg.role === 'tool' ? (
                    <div>
                      <div className="font-bold uppercase tracking-widest text-[10px] mb-4 opacity-60">{msg.content}</div>
                      <MediaPreview tool={msg.toolDetails?.tool} args={msg.toolDetails?.args} />
                      {msg.toolDetails?.args && (
                        <div className="border border-[#121212]/10 p-3 mb-2 overflow-x-auto bg-white/50">
                          <span className="opacity-50 block mb-2 uppercase tracking-widest text-[9px]">Arguments</span>
                          <pre className="whitespace-pre-wrap break-all">{JSON.stringify(msg.toolDetails.args, null, 2)}</pre>
                        </div>
                      )}
                      {msg.toolDetails?.result && (
                        <div className="border border-[#121212]/10 p-3 overflow-x-auto bg-white/50">
                          <span className="opacity-50 block mb-2 uppercase tracking-widest text-[9px]">Result</span>
                          <pre className="whitespace-pre-wrap break-all">{JSON.stringify(msg.toolDetails.result, null, 2)}</pre>
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="markdown-body font-light leading-relaxed">
                      <ReactMarkdown>{msg.content}</ReactMarkdown>
                    </div>
                  )}
                </div>
              </div>
            ))
          )}
          {isTyping && (
            <div className="flex gap-6">
              <div className="w-10 h-10 border border-[#121212]/20 flex items-center justify-center text-[#121212] flex-shrink-0">
                <Bot size={18} strokeWidth={1.5} />
              </div>
              <div className="border border-[#121212]/10 p-6 flex items-center gap-2 bg-white/50">
                <div className="w-1.5 h-1.5 bg-[#121212]/40 rounded-full animate-bounce"></div>
                <div className="w-1.5 h-1.5 bg-[#121212]/40 rounded-full animate-bounce" style={{ animationDelay: '0.2s' }}></div>
                <div className="w-1.5 h-1.5 bg-[#121212]/40 rounded-full animate-bounce" style={{ animationDelay: '0.4s' }}></div>
              </div>
            </div>
          )}
          </div>
        </div>

        <div className="p-8 border-t border-[#121212]/10 bg-transparent">
          <div className="max-w-4xl mx-auto flex items-end gap-4 border border-[#121212]/20 bg-white p-2 focus-within:border-[#121212] transition-all">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
              placeholder="描述你想做的视频,如:用导入的素材做一个 15 秒的产品展示"
              className="flex-1 bg-transparent border-none focus:ring-0 resize-none max-h-32 min-h-[48px] p-3 text-[#121212] placeholder:text-[#121212]/30 outline-none font-light"
              rows={1}
            />
            <button onClick={handleSend} disabled={!input.trim() || isTyping}
              className="px-6 py-3 h-[48px] bg-[#121212] text-[#FDFCF8] hover:bg-[#121212]/80 disabled:opacity-50 transition-colors flex-shrink-0 uppercase tracking-widest text-[10px] font-bold">
              Submit
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
