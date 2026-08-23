/**
 * 登录页 — 多租户系统入口。
 * 无注册入口 (用户由 admin 统一创建, 对齐"不提供注册"诉求)。
 * 失败提示由后端返回的 error 字段给出; 401 已由 jsonFetch 全局拦截触发跳登录。
 */
import { useState } from 'react';
import { LogIn, Loader2 } from 'lucide-react';
import * as api from '../api';

export default function Login({ onLogin }: { onLogin: (me: api.Me) => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!username.trim() || !password) return;
    setBusy(true); setError(null);
    try {
      const me = await api.login(username.trim(), password);
      onLogin(me);
    } catch (err: any) {
      setError(err.message || '登录失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center justify-center h-screen w-full bg-[#FDFCF8] text-[#121212] font-sans">
      <form onSubmit={submit} className="w-[360px] flex flex-col gap-6">
        <div>
          <h1 className="text-4xl font-bold tracking-tighter italic font-serif">Workbench</h1>
          <div className="text-[9px] uppercase tracking-[0.3em] font-medium opacity-50 mt-2">Sign in to continue</div>
        </div>

        <div className="flex flex-col gap-4">
          <label className="flex flex-col gap-2">
            <span className="text-[10px] uppercase tracking-widest font-bold opacity-60">Username</span>
            <input
              type="text"
              value={username}
              onChange={e => setUsername(e.target.value)}
              autoFocus
              autoComplete="username"
              className="px-4 py-3 border border-[#121212]/20 focus:border-[#121212] outline-none bg-transparent text-sm"
              disabled={busy}
            />
          </label>
          <label className="flex flex-col gap-2">
            <span className="text-[10px] uppercase tracking-widest font-bold opacity-60">Password</span>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              autoComplete="current-password"
              className="px-4 py-3 border border-[#121212]/20 focus:border-[#121212] outline-none bg-transparent text-sm"
              disabled={busy}
            />
          </label>
        </div>

        {error && (
          <div className="text-xs text-red-600 border border-red-600/30 px-3 py-2 bg-red-50">{error}</div>
        )}

        <button
          type="submit"
          disabled={busy || !username.trim() || !password}
          className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-[#121212] hover:bg-[#121212]/80 disabled:opacity-40 text-[#FDFCF8] transition-colors text-[10px] uppercase tracking-widest font-bold"
        >
          {busy ? <Loader2 size={16} className="animate-spin" /> : <LogIn size={16} />}
          Sign in
        </button>

        <div className="text-[10px] opacity-40 text-center">
          没有账号? 请联系管理员创建。No self-registration.
        </div>
      </form>
    </div>
  );
}
