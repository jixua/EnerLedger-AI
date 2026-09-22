import { useCallback, useEffect, useState } from "react";
import { KeyRound, LoaderCircle, Plus, RefreshCw, UserRoundCog, X } from "lucide-react";

import { Select } from "../components/ui";
import { createUser, listUsers, resetUserPassword, updateUser } from "../lib/api";
import { useAuth } from "../state/AuthContext";

const ROLE_LABELS = {
  admin: "管理员",
  user: "普通账号",
  reviewer: "审核账号",
};
const ROLE_OPTIONS = Object.entries(ROLE_LABELS).map(([value, label]) => ({ value, label }));

const DEMO_USERS = [
  { id: 1, username: "root", role: "admin", status: "ACTIVE", last_login_at: "2026-09-22T12:00:00Z", created_at: "2026-08-01T00:00:00Z" },
  { id: 2, username: "paper-reviewer", role: "reviewer", status: "ACTIVE", last_login_at: "2026-09-21T08:30:00Z", created_at: "2026-09-01T00:00:00Z" },
  { id: 3, username: "analyst", role: "user", status: "ACTIVE", last_login_at: null, created_at: "2026-09-20T00:00:00Z" },
];

export function UsersPage() {
  const { admin } = useAuth();
  const isPreview = Boolean(admin?.preview);
  const [users, setUsers] = useState(isPreview ? DEMO_USERS : []);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [resetTarget, setResetTarget] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [form, setForm] = useState({ username: "", password: "", role: "user" });
  const [resetPassword, setResetPassword] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    if (isPreview) {
      setUsers((current) => current.length ? current : DEMO_USERS);
      setLoading(false);
      return;
    }
    try {
      setUsers(await listUsers());
    } catch (requestError) {
      setError(requestError?.message || "用户列表加载失败");
    } finally {
      setLoading(false);
    }
  }, [isPreview]);

  useEffect(() => { void load(); }, [load]);

  async function handleCreate(event) {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError("");
    try {
      const created = isPreview
        ? { id: Math.max(...users.map((item) => item.id), 0) + 1, username: form.username.trim(), role: form.role, status: "ACTIVE", last_login_at: null, created_at: new Date().toISOString() }
        : await createUser(form);
      setUsers((current) => [...current, created]);
      setCreateOpen(false);
      setForm({ username: "", password: "", role: "user" });
      setNotice(`账号 ${created.username} 已创建`);
    } catch (requestError) {
      setError(requestError?.message || "账号创建失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function patchAccount(account, changes) {
    setBusyId(account.id);
    setError("");
    setNotice("");
    try {
      const updated = isPreview ? { ...account, ...changes } : await updateUser(account.id, changes);
      setUsers((current) => current.map((item) => item.id === updated.id ? updated : item));
      setNotice(`账号 ${updated.username} 已更新`);
    } catch (requestError) {
      setError(requestError?.message || "账号更新失败");
    } finally {
      setBusyId(null);
    }
  }

  async function handleReset(event) {
    event.preventDefault();
    if (!resetTarget || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      if (!isPreview) await resetUserPassword(resetTarget.id, resetPassword);
      setResetTarget(null);
      setResetPassword("");
      setNotice(`账号 ${resetTarget.username} 的密码已重置，旧登录状态已失效`);
    } catch (requestError) {
      setError(requestError?.message || "密码重置失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="page page--users feature-page">
      <header className="knowledge-hero">
        <div className="knowledge-hero__copy">
          <h1>用户管理</h1>
          <p className="page-header__description">创建和停用账号，并分配普通、审核或管理员角色。</p>
        </div>
        <button className="button button--primary knowledge-hero__action" type="button" onClick={() => { setError(""); setCreateOpen(true); }}>
          <Plus size={16} /> 新建账号
        </button>
      </header>

      {error ? <div className="notice notice--error" role="alert"><UserRoundCog size={16} /><p>{error}</p></div> : null}
      {notice ? <div className="notice notice--success" role="status"><UserRoundCog size={16} /><p>{notice}</p></div> : null}

      <section className="panel panel--flush user-management" aria-label="账号列表">
        <header className="user-management__header">
          <div><strong>{users.length}</strong> 个账号</div>
          <button className="button button--secondary button--sm" type="button" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={loading ? "spin" : ""} size={14} />刷新
          </button>
        </header>
        {loading && users.length === 0 ? <div className="user-management__loading"><LoaderCircle className="spin" size={18} />正在加载账号</div> : (
          <div className="data-table-wrap">
            <table className="data-table user-table">
              <thead><tr><th>账号</th><th>角色</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>
                {users.map((account) => {
                  const isRoot = account.id === 1;
                  const busy = busyId === account.id;
                  return (
                    <tr key={account.id}>
                      <td><strong>{account.username}</strong></td>
                      <td>
                        <Select
                          className="user-role-select"
                          ariaLabel={`${account.username}的角色`}
                          value={account.role}
                          options={ROLE_OPTIONS}
                          disabled={busy || isRoot}
                          onChange={(role) => void patchAccount(account, { role })}
                        />
                      </td>
                      <td><span className={`user-status user-status--${account.status.toLowerCase()}`}>{account.status === "ACTIVE" ? "启用" : "已停用"}</span></td>
                      <td>
                        <div className="user-table__actions">
                          <button className="button button--secondary button--sm" type="button" onClick={() => { setResetTarget(account); setResetPassword(""); }} disabled={busy}>
                            <KeyRound size={13} />重置密码
                          </button>
                          <button
                            className={`button button--sm${account.status === "ACTIVE" ? " button--danger" : " button--secondary"}`}
                            type="button"
                            disabled={busy || isRoot}
                            onClick={() => void patchAccount(account, { status: account.status === "ACTIVE" ? "DISABLED" : "ACTIVE" })}
                          >
                            {busy ? <LoaderCircle className="spin" size={13} /> : account.status === "ACTIVE" ? "停用" : "启用"}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {createOpen ? (
        <div className="dialog-backdrop" role="presentation" onMouseDown={submitting ? undefined : () => setCreateOpen(false)}>
          <section className="dialog user-dialog" role="dialog" aria-modal="true" aria-labelledby="create-user-title" onMouseDown={(event) => event.stopPropagation()}>
            <header className="dialog__header"><div><h2 id="create-user-title">新建账号</h2><p className="dialog__subtitle">账号创建后可立即登录。</p></div><button className="icon-button" type="button" onClick={() => setCreateOpen(false)} aria-label="关闭"><X size={18} /></button></header>
            <form className="form-stack" onSubmit={handleCreate}>
              <label className="form-field"><span>用户名</span><input autoFocus required maxLength={64} value={form.username} onChange={(event) => setForm((current) => ({ ...current, username: event.target.value }))} /></label>
              <label className="form-field"><span>初始密码</span><input type="password" required minLength={8} maxLength={256} autoComplete="new-password" value={form.password} onChange={(event) => setForm((current) => ({ ...current, password: event.target.value }))} /><small>至少 8 个字符。</small></label>
              <div className="form-field"><span>角色</span><Select ariaLabel="角色" value={form.role} options={ROLE_OPTIONS} onChange={(role) => setForm((current) => ({ ...current, role }))} /></div>
              <footer className="dialog__footer"><button className="button button--ghost" type="button" onClick={() => setCreateOpen(false)} disabled={submitting}>取消</button><button className="button button--primary" type="submit" disabled={submitting}>{submitting ? <LoaderCircle className="spin" size={15} /> : <Plus size={15} />}{submitting ? "正在创建" : "创建账号"}</button></footer>
            </form>
          </section>
        </div>
      ) : null}

      {resetTarget ? (
        <div className="dialog-backdrop" role="presentation" onMouseDown={submitting ? undefined : () => setResetTarget(null)}>
          <section className="dialog user-dialog" role="dialog" aria-modal="true" aria-labelledby="reset-password-title" onMouseDown={(event) => event.stopPropagation()}>
            <header className="dialog__header"><div><h2 id="reset-password-title">重置密码</h2><p className="dialog__subtitle">账号：{resetTarget.username}。保存后其旧登录状态立即失效。</p></div><button className="icon-button" type="button" onClick={() => setResetTarget(null)} aria-label="关闭"><X size={18} /></button></header>
            <form className="form-stack" onSubmit={handleReset}>
              <label className="form-field"><span>新密码</span><input autoFocus type="password" required minLength={8} maxLength={256} autoComplete="new-password" value={resetPassword} onChange={(event) => setResetPassword(event.target.value)} /><small>至少 8 个字符。</small></label>
              <footer className="dialog__footer"><button className="button button--ghost" type="button" onClick={() => setResetTarget(null)} disabled={submitting}>取消</button><button className="button button--primary" type="submit" disabled={submitting}>{submitting ? <LoaderCircle className="spin" size={15} /> : <KeyRound size={15} />}{submitting ? "正在保存" : "重置密码"}</button></footer>
            </form>
          </section>
        </div>
      ) : null}
    </div>
  );
}

export default UsersPage;
