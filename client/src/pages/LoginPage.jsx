import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { getApiBase, setStoredAuth } from "../api/client";

export default function LoginPage() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const onSubmit = (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    const base = getApiBase();
    fetch(`${base}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    })
      .then(async (r) => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          const d = body.detail;
          throw new Error(typeof d === "string" ? d : "Login failed");
        }
        return body;
      })
      .then((body) => {
        setStoredAuth({
          token: body.access_token,
          username: body.username,
          role: body.role,
        });
        navigate("/", { replace: true });
      })
      .catch((err) => {
        setError(err.message || "Login failed");
      })
      .finally(() => setLoading(false));
  };

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="logo-box login-logo">AK07</div>
        <h1 className="login-title">Sign in</h1>
        <p className="login-hint">
          Demo: <code>AK07</code>/<code>admin</code> or <code>user-1</code>/<code>user-1</code> …{" "}
          <code>user-5</code>/<code>user-5</code>
        </p>
        <form onSubmit={onSubmit} className="login-form">
          <label className="login-label">
            Username
            <input
              className="login-input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              required
            />
          </label>
          <label className="login-label">
            Password
            <input
              className="login-input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </label>
          {error ? <div className="login-error">{error}</div> : null}
          <button type="submit" className="login-submit" disabled={loading}>
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
