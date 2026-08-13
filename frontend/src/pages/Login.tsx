// 登录页（方案 §9.2）：identifier 支持用户名或邮箱；视觉按现有样式延伸。
import { useState, type FormEvent } from "react";
import { useAuth } from "../auth/AuthContext";
import { MemoryApiError } from "../api/client";

export function LoginPage({ onGoRegister }: { onGoRegister: () => void }) {
  const { login } = useAuth();
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(identifier.trim(), password);
      // 登录成功后 AuthGate 自动切换到主界面（回到此前页面，默认首页）
    } catch (err) {
      if (err instanceof MemoryApiError) {
        setError(err.message);
      } else {
        setError("登录失败，请稍后重试");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-screen">
      <div className="auth-card card">
        <div className="brand-seal" title="格物 · Math Studio">格</div>
        <h1 className="auth-title">欢迎回来</h1>
        <p className="auth-sub">登录后继续你的数学学习记忆</p>
        <form onSubmit={submit} className="auth-form">
          <label className="auth-label" htmlFor="auth-identifier">
            用户名 / 邮箱
            <input
              id="auth-identifier"
              className="auth-input"
              value={identifier}
              onChange={(e) => setIdentifier(e.target.value)}
              placeholder="用户名或邮箱"
              autoComplete="username"
              required
            />
          </label>
          <label className="auth-label" htmlFor="auth-password">
            密码
            <input
              id="auth-password"
              className="auth-input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="密码"
              autoComplete="current-password"
              required
            />
          </label>
          {error && <div className="auth-error">{error}</div>}
          <button className="btn btn-primary auth-submit" type="submit" disabled={busy}>
            {busy ? "登录中…" : "登录"}
          </button>
        </form>
        <div className="auth-switch">
          还没有账号？
          <button className="link-btn" type="button" onClick={onGoRegister}>
            注册
          </button>
        </div>
      </div>
    </div>
  );
}
