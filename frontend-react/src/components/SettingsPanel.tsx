import { useState, useEffect } from 'react';
import { Settings, Loader2, CheckCircle2, XCircle, Save, Zap } from 'lucide-react';
import * as api from '../api';
import type { SettingField } from '../api';

const GROUP_LABELS: Record<string, string> = { llm: 'Qwen VLM / Chat', deepseek: 'DeepSeek (Chat / VLM)', asr: '语音识别 (ASR)', analysis: '分析策略', tools: '工具 (FFmpeg)' };

export default function SettingsPanel() {
  const [fields, setFields] = useState<SettingField[]>([]);
  const [loading, setLoading] = useState(true);
  const [edits, setEdits] = useState<Record<string, string | boolean>>({});
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [savingGroup, setSavingGroup] = useState<string | null>(null);
  const [groupSaveMsg, setGroupSaveMsg] = useState<Record<string, string>>({});
  const [testing, setTesting] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, api.TestResult>>({});

  const fetchSettings = async () => {
    setLoading(true);
    try { setFields(await api.getSettings()); }
    catch (err) { console.error(err); }
    finally { setLoading(false); }
  };

  useEffect(() => { fetchSettings(); }, []);

  const handleChange = (key: string, value: string | boolean) => {
    setEdits(cur => ({ ...cur, [key]: value }));
  };

  const handleSave = async () => {
    setSaving(true); setSaveMsg(null);
    try {
      const updated = await api.saveSettings(edits);
      setFields(updated);
      setEdits({});
      setGroupSaveMsg({});
      setSaveMsg('已保存, 立即生效 (无需重启)');
    } catch (err: any) {
      setSaveMsg('保存失败: ' + err.message);
    } finally { setSaving(false); }
  };

  /** 就地保存单个分组 (紧挨着 Test Connection, 免得填完就手滑忘记点顶部的总 Save) */
  const handleSaveGroup = async (group: string, keys: string[]) => {
    setSavingGroup(group);
    setGroupSaveMsg(cur => ({ ...cur, [group]: '' }));
    const subset: Record<string, string | boolean> = {};
    for (const k of keys) if (k in edits) subset[k] = edits[k];
    try {
      const updated = await api.saveSettings(subset);
      setFields(updated);
      setEdits(cur => { const n = { ...cur }; for (const k of keys) delete n[k]; return n; });
      setGroupSaveMsg(cur => ({ ...cur, [group]: '已保存, 立即生效' }));
    } catch (err: any) {
      setGroupSaveMsg(cur => ({ ...cur, [group]: '保存失败: ' + err.message }));
    } finally { setSavingGroup(null); }
  };

  const handleTest = async (group: 'llm' | 'deepseek' | 'asr' | 'tools') => {
    setTesting(group);
    setTestResults(cur => ({ ...cur, [group]: undefined as any }));
    try {
      const r = await api.testSetting(group, edits);
      setTestResults(cur => ({ ...cur, [group]: r }));
    } catch (err: any) {
      setTestResults(cur => ({ ...cur, [group]: { ok: false, error: err.message } }));
    } finally { setTesting(null); }
  };

  const groups = ['llm', 'deepseek', 'asr', 'analysis', 'tools'] as const;
  const TESTABLE: ReadonlyArray<'llm' | 'deepseek' | 'asr' | 'tools'> = ['llm', 'deepseek', 'asr', 'tools'];

  return (
    <div className="h-full w-full flex flex-col p-12 overflow-y-auto">
      <div className="flex items-center justify-between mb-12 border-b border-[#121212]/10 pb-6">
        <h2 className="text-4xl font-light italic font-serif flex items-center gap-3">
          <Settings size={24} strokeWidth={1.5} className="opacity-70" /> Settings
        </h2>
        <button onClick={handleSave} disabled={saving || Object.keys(edits).length === 0}
          className="flex items-center gap-2 px-5 py-2.5 border border-[#121212] bg-[#121212] hover:bg-transparent text-[#FDFCF8] hover:text-[#121212] transition-colors disabled:opacity-40 text-[10px] uppercase tracking-widest font-bold">
          {saving ? <Loader2 size={14} strokeWidth={2} className="animate-spin" /> : <Save size={14} strokeWidth={2} />}
          Save All
        </button>
      </div>

      {saveMsg && (
        <div className="mb-8 text-xs text-[#121212]/70 border border-[#121212]/10 bg-[#121212]/5 p-3">{saveMsg}</div>
      )}

      {loading ? (
        <div className="flex-1 flex items-center justify-center"><Loader2 className="animate-spin" size={28} strokeWidth={1.5} /></div>
      ) : (
        <div className="max-w-2xl space-y-12">
          {groups.map(group => {
            const groupFields = fields.filter(f => f.group === group);
            if (groupFields.length === 0) return null;
            const result = testResults[group];
            return (
              <div key={group} className="border border-[#121212]/10 p-8">
                <div className="flex items-center justify-between mb-6">
                  <h3 className="font-serif italic text-2xl">{GROUP_LABELS[group]}</h3>
                  <div className="flex gap-3">
                    <button onClick={() => handleSaveGroup(group, groupFields.map(f => f.key))}
                      disabled={savingGroup === group || !groupFields.some(f => f.key in edits)}
                      className="flex items-center gap-2 px-4 py-2 border border-[#121212] bg-[#121212] hover:bg-transparent text-[#FDFCF8] hover:text-[#121212] transition-colors disabled:opacity-40 text-[10px] uppercase tracking-widest font-bold">
                      {savingGroup === group ? <Loader2 size={13} strokeWidth={2} className="animate-spin" /> : <Save size={13} strokeWidth={2} />}
                      Save
                    </button>
                    {TESTABLE.includes(group as 'llm' | 'deepseek' | 'asr' | 'tools') && (
                      <button onClick={() => handleTest(group as 'llm' | 'deepseek' | 'asr' | 'tools')} disabled={testing === group}
                        className="flex items-center gap-2 px-4 py-2 border border-[#121212]/20 hover:bg-[#121212]/5 transition-colors disabled:opacity-50 text-[10px] uppercase tracking-widest font-bold">
                        {testing === group ? <Loader2 size={13} strokeWidth={2} className="animate-spin" /> : <Zap size={13} strokeWidth={2} />}
                        {group === 'tools' ? 'Test FFmpeg' : 'Test Connection'}
                      </button>
                    )}
                  </div>
                </div>

                <div className="space-y-5">
                  {groupFields.map(f => (
                    <div key={f.key}>
                      {f.type === 'bool' ? (
                        <label className="flex items-center gap-3 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={(edits[f.key] ?? f.value) as boolean}
                            onChange={(e) => handleChange(f.key, e.target.checked)}
                            className="w-4 h-4 accent-[#121212]"
                          />
                          <span className="text-sm font-light">{f.label}</span>
                        </label>
                      ) : (
                        <>
                          <label className="block text-[9px] uppercase tracking-widest opacity-50 mb-2">
                            {f.label} {f.configured && <span className="text-emerald-700">· configured</span>}
                          </label>
                          <input
                            type={f.secret ? 'password' : 'text'}
                            value={f.secret ? ((edits[f.key] as string) ?? '') : ((edits[f.key] as string) ?? (f.value as string) ?? '')}
                            onChange={(e) => handleChange(f.key, e.target.value)}
                            placeholder={f.secret ? (f.configured ? `现有: ${f.value} (留空则不修改)` : '未设置') : f.label}
                            className="w-full border border-[#121212]/20 focus:border-[#121212] outline-none px-4 py-3 bg-white text-[#121212] font-light font-mono text-sm transition-colors"
                          />
                        </>
                      )}
                    </div>
                  ))}
                </div>

                {groupSaveMsg[group] && (
                  <div className="mt-6 text-xs text-[#121212]/70 border border-[#121212]/10 bg-[#121212]/5 p-3">{groupSaveMsg[group]}</div>
                )}

                {result !== undefined && (
                  <div className={`mt-6 flex items-start gap-2 text-xs p-3 border ${result?.ok ? 'border-emerald-700/20 bg-emerald-50 text-emerald-800' : 'border-red-700/20 bg-red-50 text-red-800'}`}>
                    {result?.ok ? <CheckCircle2 size={16} strokeWidth={1.5} className="flex-shrink-0 mt-0.5" /> : <XCircle size={16} strokeWidth={1.5} className="flex-shrink-0 mt-0.5" />}
                    <span className="font-mono break-all">
                      {result?.ok ? `连接成功${result.model ? ` (model=${result.model})` : ''}${result.status ? ` (HTTP ${result.status})` : ''}${result.detail ? ` · ${result.detail}` : ''}` : `失败: ${result?.error}`}
                    </span>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
