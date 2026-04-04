import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { getApiBase, setStoredAuth } from "../api/client";

const ALLOWED_USER = "AK07";

export default function LoginPage() {
  const navigate = useNavigate();
  const [username, setUsername] = useState(ALLOWED_USER);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const onSubmit = (e) => {
    e.preventDefault();
    setError("");
    if ((username || "").trim() !== ALLOWED_USER) {
      setError("Only the AK07 account can sign in.");
      return;
    }
    setLoading(true);
    const base = getApiBase();
    fetch(`${base}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: ALLOWED_USER, password }),
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
          Use username <code>AK07</code> and the password configured on the server (
          <code>AK07_PASSWORD</code> when the account is first created).
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
