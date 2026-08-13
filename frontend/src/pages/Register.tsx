// 注册页（方案 §9.2）：邮箱选填；注册成功不自动登录，跳转登录页。
import { useState, type FormEvent } from "react";
import { register } from "../api/auth";
import { MemoryApiError } from "../api/client";

export function RegisterPage({ onGoLogin }: { onGoLogin: () => void }) {
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await register(username.trim(), password, email.trim() || undefined);
      setDone(true);
    } catch (err) {
      if (err instanceof MemoryApiError) {
        setError(err.message);
      } else {
        setError("注册失败，请稍后重试");
      }
    } finally {
      setBusy(false);
    }
  };

  if (done) {
    return (
      <div className="auth-screen">
        <div className="auth-card card">
          <div className="brand-seal" title="格物 · Math Studio">格</div>
          <h1 className="auth-title">注册成功</h1>
          <p className="auth-sub">请使用新账号登录</p>
          <button className="btn btn-primary auth-submit" type="button" onClick={onGoLogin}>
            去登录
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="auth-screen">
      <div className="auth-card card">
        <div className="brand-seal" title="格物 · Math Studio">格</div>
        <h1 className="auth-title">创建账号</h1>
        <p className="auth-sub">注册后，AI 将记住你的每一次学习</p>
        <form onSubmit={submit} className="auth-form">
          <label className="auth-label" htmlFor="reg-username">
            用户名
            <input
              id="reg-username"
              className="auth-input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="3–64 位小写字母、数字或下划线"
              autoComplete="username"
              required
            />
          </label>
          <label className="auth-label" htmlFor="reg-email">
            邮箱（选填）
            <input
              id="reg-email"
              className="auth-input"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="用于找回账号（暂未开放）"
              autoComplete="email"
            />
          </label>
          <label className="auth-label" htmlFor="reg-password">
            密码
            <input
              id="reg-password"
              className="auth-input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="至少 10 位，含字母与数字"
              autoComplete="new-password"
              required
            />
          </label>
          {error && <div className="auth-error">{error}</div>}
          <button className="btn btn-primary auth-submit" type="submit" disabled={busy}>
            {busy ? "注册中…" : "注册"}
          </button>
        </form>
        <div className="auth-switch">
          已有账号？
          <button className="link-btn" type="button" onClick={onGoLogin}>
            登录
          </button>
        </div>
      </div>
    </div>
  );
}
