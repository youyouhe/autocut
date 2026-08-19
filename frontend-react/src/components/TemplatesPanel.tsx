import { useState, useEffect } from 'react';
import { LayoutTemplate, Loader2, RefreshCw, Sparkles } from 'lucide-react';
import * as api from '../api';
import type { TemplateInfo } from '../api';

interface Props {
  setDraftId: (id: string | null) => void;
  onGenerated: () => void;   // 生成后切到 Drafts tab
}

export default function TemplatesPanel({ setDraftId, onGenerated }: Props) {
  const [templates, setTemplates] = useState<TemplateInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<TemplateInfo | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchTemplates = async () => {
    setLoading(true);
    try { setTemplates(await api.listTemplates()); }
    catch (err) { console.error(err); }
    finally { setLoading(false); }
  };

  useEffect(() => { fetchTemplates(); }, []);

  const handleSelect = (t: TemplateInfo) => {
    setSelected(t);
    setError(null);
    setValues(Object.fromEntries(t.variables.map(v => [v, ''])));
  };

  const handleGenerate = async () => {
    if (!selected) return;
    setGenerating(true);
    setError(null);
    try {
      const r = await api.renderTemplate(selected.file, values);
      if (r.error) { setError(r.error); return; }
      if (r.draft_id) {
        setDraftId(r.draft_id);
        onGenerated();
      }
    } catch (err: any) {
      setError(err.message);
    } finally { setGenerating(false); }
  };

  return (
    <div className="h-full w-full flex overflow-hidden">
      {/* 模板列表 */}
      <div className="w-96 border-r border-[#121212]/10 flex flex-col overflow-y-auto p-8">
        <div className="flex items-center justify-between mb-8 border-b border-[#121212]/10 pb-4">
          <h2 className="text-2xl font-light italic font-serif">Templates</h2>
          <button onClick={fetchTemplates} className="p-2 border border-[#121212]/20 hover:bg-[#121212]/5 transition-colors">
            <RefreshCw size={14} strokeWidth={1.5} />
          </button>
        </div>
        {loading ? (
          <div className="flex-1 flex items-center justify-center"><Loader2 className="animate-spin" size={24} strokeWidth={1.5} /></div>
        ) : templates.length === 0 ? (
          <p className="text-[10px] uppercase tracking-widest opacity-40">No templates found</p>
        ) : (
          <div className="space-y-3">
            {templates.map(t => (
              <button key={t.file} onClick={() => handleSelect(t)}
                className={`w-full text-left p-4 border transition-colors ${selected?.file === t.file ? 'border-[#121212] bg-[#121212]/5' : 'border-[#121212]/10 hover:border-[#121212]/30'}`}>
                <div className="flex items-center gap-2 mb-1">
                  <LayoutTemplate size={14} strokeWidth={1.5} className="opacity-50" />
                  <span className="font-serif italic text-lg">{t.name}</span>
                </div>
                <p className="text-[10px] uppercase tracking-widest opacity-50 leading-relaxed">{t.description}</p>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* 变量表单 */}
      <div className="flex-1 overflow-y-auto p-12">
        {!selected ? (
          <div className="h-full flex flex-col items-center justify-center text-[#121212]/50">
            <LayoutTemplate size={48} className="mb-4 opacity-20" strokeWidth={1} />
            <p className="font-serif italic text-2xl">Select a template</p>
          </div>
        ) : (
          <div className="max-w-xl">
            <h3 className="font-serif italic text-3xl mb-2">{selected.name}</h3>
            <p className="text-[10px] uppercase tracking-widest opacity-50 mb-8">{selected.description}</p>

            {selected.variables.length === 0 ? (
              <p className="text-[10px] uppercase tracking-widest opacity-40 mb-8">No variables to fill — ready to generate.</p>
            ) : (
              <div className="space-y-5 mb-8">
                {selected.variables.map(v => (
                  <div key={v}>
                    <label className="block text-[9px] uppercase tracking-widest opacity-50 mb-2">{v}</label>
                    <input
                      value={values[v] ?? ''}
                      onChange={(e) => setValues(cur => ({ ...cur, [v]: e.target.value }))}
                      className="w-full border border-[#121212]/20 focus:border-[#121212] outline-none px-4 py-3 bg-white text-[#121212] font-light transition-colors"
                      placeholder={v}
                    />
                  </div>
                ))}
              </div>
            )}

            {error && (
              <div className="mb-6 text-xs text-red-700 border border-red-700/20 bg-red-50 p-3 font-mono break-all">{error}</div>
            )}

            <button onClick={handleGenerate} disabled={generating}
              className="flex items-center gap-2 px-6 py-3 border border-[#121212] bg-[#121212] hover:bg-transparent text-[#FDFCF8] hover:text-[#121212] transition-colors disabled:opacity-50 text-[10px] uppercase tracking-widest font-bold">
              {generating ? <Loader2 size={14} strokeWidth={2} className="animate-spin" /> : <Sparkles size={14} strokeWidth={2} />}
              Generate Draft
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
