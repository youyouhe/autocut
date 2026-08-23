/**
 * AdminPanel — 用户管理 (仅 admin 可见).
 * 后端契约 (/api/admin/users): GET → {users:[...]}, POST → {user:{...}} 201,
 *   DELETE /<id> → {ok}, POST /<id>/password → {ok}, POST /<id>/admin {is_admin} → {ok}.
 * 无注册入口 — admin 在此统一创建用户, 对齐"用户统一由 admin 管理, 不提供注册".
 */
import { useEffect, useState } from 'react';
import { Users, UserPlus, Trash2, KeyRound, ShieldCheck, Shield, Loader2, X } from 'lucide-react';
import * as api from '../api';

export default function AdminPanel() {
  const [users, setUsers] = useState<api.UserRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  // 新建用户弹层
  const [showCreate, setShowCreate] = useState(false);
  const [nu, setNu] = useState({ username: '', password: '', display_name: '', is_admin: false });
  const [createErr, setCreateErr] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  // 改密码弹层
  const [pwdFor, setPwdFor] = useState<api.UserRow | null>(null);
  const [newPwd, setNewPwd] = useState('');
  const [pwdErr, setPwdErr] = useState<string | null>(null);
  const [pwdBusy, setPwdBusy] = useState(false);
  const [nodes, setNodes] = useState<any[]>([]);

  const refresh = async () => {
    setLoading(true); setError(null);
    try {
      setUsers(await api.listUsers());
      api.getRenderNodes().then(setNodes).catch(() => {});
    }
    catch (e: any) { setError(e.message || '加载用户失败'); }
    finally { setLoading(false); }
  };

  useEffect(() => { refresh(); }, []);

  const doCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!nu.username.trim() || !nu.password) return;
    setCreating(true); setCreateErr(null);
    try {
      await api.createUser(nu.username.trim(), nu.password, nu.display_name.trim() || undefined, nu.is_admin);
      setShowCreate(false);
      setNu({ username: '', password: '', display_name: '', is_admin: false });
      await refresh();
    } catch (err: any) { setCreateErr(err.message || '创建失败'); }
    finally { setCreating(false); }
  };

  const doDelete = async (u: api.UserRow) => {
    if (!confirm(`确认删除用户 "${u.username}"?\n该用户的素材/草稿/对话记录将保留, 但其账号无法再登录。`)) return;
    setBusyId(u.id); setError(null);
    try { await api.deleteUser(u.id); await refresh(); }
    catch (e: any) { setError(e.message || '删除失败'); }
    finally { setBusyId(null); }
  };

  const doToggleAdmin = async (u: api.UserRow) => {
    setBusyId(u.id); setError(null);
    try {
      await api.setUserAdmin(u.id, !u.is_admin);
      await refresh();
    } catch (e: any) { setError(e.message || '修改失败'); }
    finally { setBusyId(null); }
  };

  const doChangePwd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!pwdFor || !newPwd) return;
    setPwdBusy(true); setPwdErr(null);
    try {
      await api.updateUserPassword(pwdFor.id, newPwd);
      setPwdFor(null); setNewPwd('');
    } catch (err: any) { setPwdErr(err.message || '改密失败'); }
    finally { setPwdBusy(false); }
  };

  const fmtDate = (t?: number) => t ? new Date(t * 1000).toLocaleString('zh-CN', { hour12: false }) : '-';

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
            <Users size={20} /> 用户管理
          </h1>
          <div className="text-[10px] uppercase tracking-widest opacity-50 mt-1">
            Admin-only · 用户统一由管理员创建, 不开放注册
          </div>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="flex items-center gap-2 px-4 py-2 bg-[#121212] hover:bg-[#121212]/80 text-[#FDFCF8] text-[10px] uppercase tracking-widest font-bold transition-colors"
        >
          <UserPlus size={14} /> 新建用户
        </button>
      </div>

      {error && (
        <div className="text-xs text-red-600 border border-red-600/30 px-3 py-2 bg-red-50 mb-4">{error}</div>
      )}

      {loading ? (
        <div className="flex items-center gap-2 text-sm opacity-60 py-8 justify-center">
          <Loader2 size={16} className="animate-spin" /> 加载中...
        </div>
      ) : (
        <div className="border border-[#121212]/10 overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-[#121212]/5 text-[10px] uppercase tracking-widest opacity-70">
              <tr>
                <th className="text-left px-4 py-3 font-bold">用户名</th>
                <th className="text-left px-4 py-3 font-bold">显示名</th>
                <th className="text-left px-4 py-3 font-bold">角色</th>
                <th className="text-left px-4 py-3 font-bold">创建时间</th>
                <th className="text-right px-4 py-3 font-bold">操作</th>
              </tr>
            </thead>
            <tbody>
              {users.map(u => (
                <tr key={u.id} className="border-t border-[#121212]/10 hover:bg-[#121212]/[0.02]">
                  <td className="px-4 py-3 font-mono">{u.username}</td>
                  <td className="px-4 py-3 opacity-70">{u.display_name || '-'}</td>
                  <td className="px-4 py-3">
                    {u.is_admin ? (
                      <span className="inline-flex items-center gap-1 text-[10px] uppercase tracking-wider font-bold text-emerald-700 bg-emerald-50 border border-emerald-600/30 px-2 py-1">
                        <ShieldCheck size={11} /> Admin
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-[10px] uppercase tracking-wider font-bold opacity-50 border border-[#121212]/20 px-2 py-1">
                        <Shield size={11} /> User
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 opacity-60 text-xs whitespace-nowrap">{fmtDate(u.created_at)}</td>
                  <td className="px-4 py-3">
                    <div className="flex items-center justify-end gap-2">
                      <button
                        onClick={() => { setPwdFor(u); setNewPwd(''); setPwdErr(null); }}
                        title="重置密码"
                        className="p-2 hover:bg-[#121212]/10 transition-colors"
                        disabled={busyId === u.id}
                      >
                        <KeyRound size={14} />
                      </button>
                      <button
                        onClick={() => doToggleAdmin(u)}
                        title={u.is_admin ? '取消管理员' : '设为管理员'}
                        className={`p-2 transition-colors ${u.is_admin ? 'text-emerald-700 hover:bg-emerald-50' : 'hover:bg-[#121212]/10'}`}
                        disabled={busyId === u.id}
                      >
                        <ShieldCheck size={14} />
                      </button>
                      <button
                        onClick={() => doDelete(u)}
                        title="删除用户"
                        className="p-2 text-red-600 hover:bg-red-50 transition-colors"
                        disabled={busyId === u.id}
                      >
                        {busyId === u.id ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {users.length === 0 && (
                <tr><td colSpan={5} className="px-4 py-8 text-center opacity-40 text-xs">暂无用户</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* 渲染节点 (心跳互通) */}
      <div className="mt-10">
        <h2 className="text-lg font-bold tracking-tight flex items-center gap-2 mb-3">
          渲染节点 <span className="text-[10px] uppercase tracking-widest opacity-50 font-normal mt-0.5">心跳互通 · 30s 一次, 120s 无心跳判失联</span>
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {nodes.map(n => (
            <div key={n.node_id} className={`border p-4 ${n.online ? 'border-emerald-600/40 bg-emerald-50/40' : 'border-red-600/40 bg-red-50/40'}`}>
              <div className="flex items-center justify-between mb-2">
                <span className="font-mono text-xs truncate" title={n.node_id}>{n.node_id}</span>
                <span className={`text-[10px] uppercase tracking-widest font-bold px-2 py-0.5 ${n.online ? 'text-emerald-700 bg-emerald-100' : 'text-red-700 bg-red-100'}`}>
                  {n.online ? '● 在线' : '○ 离线'}
                </span>
              </div>
              <div className="text-[10px] uppercase tracking-widest opacity-60 space-y-1">
                {n.desktops_total !== undefined ? (
                  <div>桌面池 {n.desktops_busy}/{n.desktops_total} 忙 · 队列 {n.queue_size ?? 0}</div>
                ) : (
                  <div className="opacity-70 normal-case">{n.note || '无心跳数据'}</div>
                )}
                <div>{n.running_tasks?.length ? `渲染中: ${n.running_tasks.length} 个任务` : '空闲'}</div>
              </div>
            </div>
          ))}
          {nodes.length === 0 && <div className="text-xs opacity-50">暂无节点信息</div>}
        </div>
      </div>

      {/* 新建用户弹层 */}
      {showCreate && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" onClick={() => setShowCreate(false)}>
          <form onSubmit={doCreate} onClick={e => e.stopPropagation()} className="bg-[#FDFCF8] w-[380px] p-6 flex flex-col gap-4 shadow-xl">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-bold flex items-center gap-2"><UserPlus size={16} /> 新建用户</h2>
              <button type="button" onClick={() => setShowCreate(false)} className="p-1 hover:bg-[#121212]/10"><X size={16} /></button>
            </div>
            <label className="flex flex-col gap-1">
              <span className="text-[10px] uppercase tracking-widest font-bold opacity-60">用户名 *</span>
              <input value={nu.username} onChange={e => setNu({ ...nu, username: e.target.value })}
                className="px-3 py-2 border border-[#121212]/20 focus:border-[#121212] outline-none bg-transparent text-sm" autoFocus />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[10px] uppercase tracking-widest font-bold opacity-60">初始密码 *</span>
              <input type="password" value={nu.password} onChange={e => setNu({ ...nu, password: e.target.value })}
                className="px-3 py-2 border border-[#121212]/20 focus:border-[#121212] outline-none bg-transparent text-sm" />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[10px] uppercase tracking-widest font-bold opacity-60">显示名 (可选)</span>
              <input value={nu.display_name} onChange={e => setNu({ ...nu, display_name: e.target.value })}
                className="px-3 py-2 border border-[#121212]/20 focus:border-[#121212] outline-none bg-transparent text-sm" />
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={nu.is_admin} onChange={e => setNu({ ...nu, is_admin: e.target.checked })} />
              <span className="text-sm">设为管理员</span>
            </label>
            {createErr && <div className="text-xs text-red-600 border border-red-600/30 px-3 py-2 bg-red-50">{createErr}</div>}
            <button type="submit" disabled={creating || !nu.username.trim() || !nu.password}
              className="flex items-center justify-center gap-2 px-4 py-2.5 bg-[#121212] hover:bg-[#121212]/80 disabled:opacity-40 text-[#FDFCF8] text-[10px] uppercase tracking-widest font-bold transition-colors">
              {creating ? <Loader2 size={14} className="animate-spin" /> : <UserPlus size={14} />} 创建
            </button>
          </form>
        </div>
      )}

      {/* 重置密码弹层 */}
      {pwdFor && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" onClick={() => setPwdFor(null)}>
          <form onSubmit={doChangePwd} onClick={e => e.stopPropagation()} className="bg-[#FDFCF8] w-[380px] p-6 flex flex-col gap-4 shadow-xl">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-bold flex items-center gap-2"><KeyRound size={16} /> 重置密码</h2>
              <button type="button" onClick={() => setPwdFor(null)} className="p-1 hover:bg-[#121212]/10"><X size={16} /></button>
            </div>
            <div className="text-xs opacity-60">为用户 <span className="font-mono font-bold">{pwdFor.username}</span> 设置新密码</div>
            <label className="flex flex-col gap-1">
              <span className="text-[10px] uppercase tracking-widest font-bold opacity-60">新密码 *</span>
              <input type="password" value={newPwd} onChange={e => setNewPwd(e.target.value)} autoFocus
                className="px-3 py-2 border border-[#121212]/20 focus:border-[#121212] outline-none bg-transparent text-sm" />
            </label>
            {pwdErr && <div className="text-xs text-red-600 border border-red-600/30 px-3 py-2 bg-red-50">{pwdErr}</div>}
            <button type="submit" disabled={pwdBusy || !newPwd}
              className="flex items-center justify-center gap-2 px-4 py-2.5 bg-[#121212] hover:bg-[#121212]/80 disabled:opacity-40 text-[#FDFCF8] text-[10px] uppercase tracking-widest font-bold transition-colors">
              {pwdBusy ? <Loader2 size={14} className="animate-spin" /> : <KeyRound size={14} />} 确认重置
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
