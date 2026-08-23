/**
 * per-user 自定义 Render 节点配置面板
 *
 * 每个用户自助配置自己的 render_service (URL + X-Render-Token): 渲染优先走自己的 CapCut,
 * 提交失败/未配置时回退公共节点. 与 SettingsPanel (admin-only 平台级密钥) 不同 ——
 * 这是 per-user 用户自助, 非 admin 也能配. 仿 SettingsPanel 的表单样式.
 */
import { useState, useEffect } from 'react';
import { Server, Loader2, CheckCircle2, XCircle, Save, Zap } from 'lucide-react';
import * as api from '../api';
import type { RenderConfig, RenderConfigTestResult } from '../api';

export default function RenderNodePanel() {
  const [loading, setLoading] = useState(true);
  const [configured, setConfigured] = useState(false);
  const [publicUrl, setPublicUrl] = useState('');
  const [url, setUrl] = useState('');
  const [token, setToken] = useState('');          // 仅在用户本次填写时有值 (留空=不修改)
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<RenderConfigTestResult | null>(null);

  const fetchConfig = async () => {
    setLoading(true);
    try {
      const cfg: RenderConfig = await api.getRenderConfig();
      setConfigured(cfg.configured);
      setPublicUrl(cfg.public_url || '');
      setUrl(cfg.url || '');
      setToken('');   // token 永远不回填明文 (后端只给脱敏值)
    } catch (err: any) {
      setSaveMsg('加载配置失败: ' + err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchConfig(); }, []);

  const handleSave = async () => {
    setSaving(true); setSaveMsg(null);
    try {
      const cfg: RenderConfig = await api.saveRenderConfig(url, token || undefined);
      setConfigured(cfg.configured);
      setUrl(cfg.url || '');
      setToken('');
      setSaveMsg(cfg.configured ? '已保存, 下次渲染优先走你的节点' : '已清空, 将使用公共节点');
    } catch (err: any) {
      setSaveMsg('保存失败: ' + err.message);
    } finally { setSaving(false); }
  };

  const handleTest = async () => {
    setTesting(true); setTestResult(null);
    try {
      const r = await api.testRenderConfig(url, token || undefined);
      setTestResult(r);
    } catch (err: any) {
      setTestResult({ ok: false, error: err.message });
    } finally { setTesting(false); }
  };

  return (
    <div className="h-full w-full flex flex-col p-12 overflow-y-auto">
      <div className="flex items-center justify-between mb-12 border-b border-[#121212]/10 pb-6">
        <h2 className="text-4xl font-light italic font-serif flex items-center gap-3">
          <Server size={24} strokeWidth={1.5} className="opacity-70" /> Render Node
        </h2>
        <button onClick={handleSave} disabled={saving || !url.trim()}
          className="flex items-center gap-2 px-5 py-2.5 border border-[#121212] bg-[#121212] hover:bg-transparent text-[#FDFCF8] hover:text-[#121212] transition-colors disabled:opacity-40 text-[10px] uppercase tracking-widest font-bold">
          {saving ? <Loader2 size={14} strokeWidth={2} className="animate-spin" /> : <Save size={14} strokeWidth={2} />}
          Save
        </button>
      </div>

      {/* 当前状态 + 公共节点提示 */}
      <div className="max-w-2xl mb-8 text-xs border border-[#121212]/10 bg-[#121212]/5 p-4 space-y-1">
        <div>
          当前状态:
          {configured
            ? <span className="ml-2 text-emerald-700 font-medium">已配置自有节点 — 渲染优先走你的 CapCut</span>
            : <span className="ml-2 opacity-70">未配置 — 使用公共节点</span>}
        </div>
        {publicUrl && <div className="opacity-60 font-mono">公共节点 (兜底): {publicUrl}</div>}
        <div className="opacity-50 pt-1">
          渲染时优先调用你的 render_service; 连不上或提交失败时自动回退公共节点, 并在任务上标注回退原因.
          将 URL 清空保存即可恢复走公共节点.
        </div>
      </div>

      {saveMsg && (
        <div className="mb-8 text-xs text-[#121212]/70 border border-[#121212]/10 bg-[#121212]/5 p-3">{saveMsg}</div>
      )}

      {loading ? (
        <div className="flex-1 flex items-center justify-center"><Loader2 className="animate-spin" size={28} strokeWidth={1.5} /></div>
      ) : (
        <div className="max-w-2xl space-y-8">
          <div className="border border-[#121212]/10 p-8">
            <div className="flex items-center justify-between mb-6">
              <h3 className="font-serif italic text-2xl">我的节点</h3>
              <button onClick={handleTest} disabled={testing || !url.trim()}
                className="flex items-center gap-2 px-4 py-2 border border-[#121212]/20 hover:bg-[#121212]/5 transition-colors disabled:opacity-50 text-[10px] uppercase tracking-widest font-bold">
                {testing ? <Loader2 size={13} strokeWidth={2} className="animate-spin" /> : <Zap size={13} strokeWidth={2} />}
                测试连接
              </button>
            </div>

            <div className="space-y-5">
              <div>
                <label className="block text-[9px] uppercase tracking-widest opacity-50 mb-2">
                  Render Service URL
                </label>
                <input
                  type="text"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="http://<渲染节点IP>:9020"
                  className="w-full border border-[#121212]/20 focus:border-[#121212] outline-none px-4 py-3 bg-white text-[#121212] font-light font-mono text-sm transition-colors"
                />
              </div>
              <div>
                <label className="block text-[9px] uppercase tracking-widest opacity-50 mb-2">
                  X-Render-Token {configured && <span className="text-emerald-700">· 已配置</span>}
                </label>
                <input
                  type="password"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  placeholder={configured ? '留空则不修改' : '可选 (你的 render_service 若设了 token)'}
                  className="w-full border border-[#121212]/20 focus:border-[#121212] outline-none px-4 py-3 bg-white text-[#121212] font-light font-mono text-sm transition-colors"
                />
              </div>
            </div>

            {testResult && (
              <div className={`mt-6 flex items-start gap-2 text-xs p-3 border ${testResult.ok ? 'border-emerald-700/20 bg-emerald-50 text-emerald-800' : 'border-red-700/20 bg-red-50 text-red-800'}`}>
                {testResult.ok ? <CheckCircle2 size={16} strokeWidth={1.5} className="flex-shrink-0 mt-0.5" /> : <XCircle size={16} strokeWidth={1.5} className="flex-shrink-0 mt-0.5" />}
                <span className="font-mono break-all">
                  {testResult.ok
                    ? `连接成功${testResult.detail ? ` · ${testResult.detail}` : ''}${testResult.desktops ? ` · desktops: ${testResult.desktops.join(', ')}` : ''}`
                    : `失败: ${testResult.error}`}
                </span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
