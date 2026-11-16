import { useEffect, useState, type ReactNode } from "react";

export function AuthGate({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [allowed, setAllowed] = useState(false);
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    fetch("/api/auth/status").then(response => {
      if (!response.ok) throw new Error("服务暂不可用");
      return response.json();
    }).then(value => setAllowed(value.authenticated === true)).catch(() => setError("无法连接服务，请稍后重试。")).finally(() => setReady(true));
  }, []);
  if (!ready) return <main style={{ padding: 32 }}>正在连接…</main>;
  if (allowed) return <>{children}</>;
  return <main style={{ maxWidth: 480, margin: "64px auto", padding: 24 }}>
    <h1>Velora 管理界面</h1>
    <p>输入本机访问密钥后继续。可在本机终端运行 <code>velora --show-access-token</code> 获取。</p>
    <form onSubmit={async event => {
      event.preventDefault();
      setError("");
      try {
        const response = await fetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
        if (!response.ok) throw new Error();
        setToken("");
        setAllowed(true);
      } catch { setError("密钥不正确或登录不可用，请检查后重试。"); }
    }}>
      <label>本机访问密钥 <input type="password" autoComplete="off" required value={token} onChange={event => setToken(event.target.value)} /></label>
      <button className="btn" type="submit">进入管理界面</button>
    </form>
    {error && <p role="alert">{error}</p>}
  </main>;
}
