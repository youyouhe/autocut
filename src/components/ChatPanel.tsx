import { useState, useRef, useEffect } from 'react';
import { Send, Bot, User, SquareTerminal } from 'lucide-react';
import ReactMarkdown from 'react-markdown';

type Message = {
  role: 'user' | 'assistant' | 'tool';
  content: string;
  toolDetails?: any;
};

export default function ChatPanel() {
  const [messages, setMessages] = useState<Message[]>([
    { role: 'assistant', content: 'Hello! I am your AI Video Assistant. How can I help you produce your video today?' }
  ]);
  const [input, setInput] = useState('');
  const [isTyping, setIsTyping] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const handleSend = async () => {
    if (!input.trim()) return;
    
    const userMsg = input.trim();
    setInput('');
    setMessages(prev => [...prev, { role: 'user', content: userMsg }]);
    setIsTyping(true);

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: userMsg })
      });

      if (!response.body) throw new Error("No response body");

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let assistantContent = '';
      
      setMessages(prev => [...prev, { role: 'assistant', content: '' }]);

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        
        const chunk = decoder.decode(value);
        const lines = chunk.split('\n');
        
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const dataStr = line.substring(6).trim();
            if (dataStr === '[DONE]') {
              setIsTyping(false);
              return;
            }
            
            try {
              const data = JSON.parse(dataStr);
              if (data.text) {
                assistantContent += data.text;
                setMessages(prev => {
                  const newMsgs = [...prev];
                  newMsgs[newMsgs.length - 1].content = assistantContent;
                  return newMsgs;
                });
              } else if (data.tool) {
                setMessages(prev => [
                  ...prev, 
                  { 
                    role: 'tool', 
                    content: `Invoked tool: ${data.tool}`,
                    toolDetails: data
                  },
                  { role: 'assistant', content: assistantContent }
                ]);
              }
            } catch (e) {
              // Ignore parse errors for incomplete chunks
            }
          }
        }
      }
    } catch (err) {
      console.error(err);
    } finally {
      setIsTyping(false);
    }
  };

  return (
    <div className="h-full w-full flex flex-col bg-transparent">
      <div className="p-8 border-b border-[#121212]/10 z-10 flex justify-between items-center">
        <h2 className="text-3xl font-light italic font-serif">AI Assistant</h2>
        <div className="text-[10px] uppercase tracking-widest font-bold border border-[#121212]/10 px-3 py-1 text-[#121212]">
          Draft Context: Active
        </div>
      </div>
      
      <div className="flex-1 overflow-y-auto p-8 space-y-8">
        {messages.map((msg, idx) => (
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
                  {msg.toolDetails?.args && (
                    <div className="border border-[#121212]/10 p-3 mb-2 overflow-x-auto bg-white/50">
                      <span className="opacity-50 block mb-2 uppercase tracking-widest text-[9px]">Arguments</span>
                      {JSON.stringify(msg.toolDetails.args, null, 2)}
                    </div>
                  )}
                  {msg.toolDetails?.result && (
                    <div className="border border-[#121212]/10 p-3 overflow-x-auto bg-white/50">
                      <span className="opacity-50 block mb-2 uppercase tracking-widest text-[9px]">Result</span>
                      {JSON.stringify(msg.toolDetails.result, null, 2)}
                    </div>
                  )}
                </div>
              ) : (
                <div className={msg.role === 'user' ? 'prose-invert font-light leading-relaxed' : 'markdown-body prose prose-slate max-w-none font-light leading-relaxed'}>
                  <ReactMarkdown>{msg.content}</ReactMarkdown>
                </div>
              )}
            </div>
          </div>
        ))}
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
        <div ref={messagesEndRef} />
      </div>

      <div className="p-8 border-t border-[#121212]/10 bg-transparent">
        <div className="max-w-4xl mx-auto flex items-end gap-4 border border-[#121212]/20 bg-white p-2 focus-within:border-[#121212] transition-all">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            placeholder="Describe what video you want to make..."
            className="flex-1 bg-transparent border-none focus:ring-0 resize-none max-h-32 min-h-[48px] p-3 text-[#121212] placeholder:text-[#121212]/30 outline-none font-light"
            rows={1}
          />
          <button
            onClick={handleSend}
            disabled={!input.trim() || isTyping}
            className="px-6 py-3 h-[48px] bg-[#121212] text-[#FDFCF8] hover:bg-[#121212]/80 disabled:opacity-50 transition-colors flex-shrink-0 uppercase tracking-widest text-[10px] font-bold"
          >
            Submit
          </button>
        </div>
      </div>
    </div>
  );
}
