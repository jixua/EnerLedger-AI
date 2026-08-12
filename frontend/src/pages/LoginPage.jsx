import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { BookOpenCheck, CircleAlert, Leaf, LoaderCircle, LockKeyhole, ScanSearch, ShieldCheck, UserRound } from "lucide-react";

import { useAuth } from "../state/AuthContext";

export function LoginPage() {
  const { authenticated, checking, login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const location = useLocation();
  const navigate = useNavigate();

  if (!checking && authenticated) return <Navigate to="/" replace />;

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await login(username.trim(), password);
      const destination = location.state?.from;
      navigate(typeof destination === "string" && destination.startsWith("/") ? destination : "/", { replace: true });
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "登录失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-page">
      <aside className="login-story" aria-label="产品能力介绍">
        <div className="login-story__brand"><Leaf size={19} /><span>能碳会计 · EnerLedger AI</span></div>
        <div className="login-story__content">
          <p className="eyebrow">Carbon intelligence workspace</p>
          <h1>让碳标准成为<br />可追溯的答案。</h1>
          <p>从复杂文档解析、结构还原到多源检索，把分散的政策、标准和方法学整理成可核验的知识底座。</p>
          <ul>
            <li><BookOpenCheck size={17} /><span><strong>标准入库</strong><small>Word、PDF 与复杂表格结构化</small></span></li>
            <li><ScanSearch size={17} /><span><strong>证据检索</strong><small>答案直接回溯原文与页码</small></span></li>
            <li><ShieldCheck size={17} /><span><strong>解析可控</strong><small>质量状态、任务进度清晰可见</small></span></li>
          </ul>
        </div>
        <p className="login-story__note">Knowledge infrastructure for carbon accounting</p>
      </aside>
      <section className="login-card" aria-labelledby="login-title">
        <header className="login-brand">
          <span><Leaf size={23} /></span>
          <div><strong>能碳会计</strong><small>AI 智能体</small></div>
        </header>
        <div className="login-heading">
          <h1 id="login-title">管理员登录</h1>
          <p>进入碳知识库与文档解析工作台。</p>
        </div>
        <form onSubmit={submit} className="login-form">
          <label>
            <span>用户名</span>
            <div><UserRound size={17} /><input autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required autoFocus /></div>
          </label>
          <label>
            <span>密码</span>
            <div><LockKeyhole size={17} /><input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></div>
          </label>
          {error ? <p className="login-error" role="alert"><CircleAlert size={15} />{error}</p> : null}
          <button type="submit" disabled={busy || !username.trim() || !password}>
            {busy ? <LoaderCircle className="spin" size={17} /> : <LockKeyhole size={17} />}
            {busy ? "正在验证" : "登录"}
          </button>
        </form>
        <footer>系统未开放自助注册，仅限授权管理员使用。</footer>
      </section>
    </main>
  );
}

export default LoginPage;
