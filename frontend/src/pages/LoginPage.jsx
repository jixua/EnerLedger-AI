import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { CircleAlert, Leaf, LoaderCircle, LockKeyhole, UserRound } from "lucide-react";

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
      <section className="login-card" aria-labelledby="login-title">
        <header className="login-brand">
          <span><Leaf size={23} /></span>
          <div><strong>能碳会计</strong><small>AI 智能体</small></div>
        </header>
        <div className="login-heading">
          <h1 id="login-title">管理员登录</h1>
          <p>登录后管理数据集、模型配置与文档解析任务。</p>
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
