import { useCallback, useEffect, useState } from "react";

const API_BASE =
  (import.meta.env.VITE_DASHBOARD_API_BASE || "").trim() || "http://127.0.0.1:8000";

function formatApiErrorDetail(detail) {
  if (detail == null) {
    return "";
  }
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail
      .map((item) => (item && (item.msg || item.message)) || JSON.stringify(item))
      .join("; ");
  }
  return String(detail);
}

export function UpstoxSettingsCard() {
  const [baseUrl, setBaseUrl] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [adminToken, setAdminToken] = useState(() =>
    typeof window !== "undefined" ? window.localStorage.getItem("dashboardAdminToken") || "" : ""
  );
  const [previews, setPreviews] = useState({
    access_token_preview: "",
    api_key_preview: "",
    api_secret_preview: "",
  });
  const [flags, setFlags] = useState({
    has_access_token: false,
    admin_token_configured: false,
  });
  const [credentialsFile, setCredentialsFile] = useState("");
  const [credentialsPath, setCredentialsPath] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true);

  const loadSettings = useCallback(() => {
    setLoading(true);
    fetch(`${API_BASE}/api/settings/upstox`)
      .then((r) => r.json())
      .then((data) => {
        setBaseUrl(data.base_url || "");
        setPreviews({
          access_token_preview: data.access_token_preview || "",
          api_key_preview: data.api_key_preview || "",
          api_secret_preview: data.api_secret_preview || "",
        });
        setFlags({
          has_access_token: Boolean(data.has_access_token),
          admin_token_configured: Boolean(data.admin_token_configured),
        });
        setCredentialsFile(data.credentials_file || "");
        setCredentialsPath(data.credentials_path || "");
        setStatus("");
      })
      .catch((e) => {
        console.error(e);
        setStatus("Could not load settings (is the API running on port 8000?)");
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  const persistAdminToken = (value) => {
    setAdminToken(value);
    if (typeof window !== "undefined") {
      window.localStorage.setItem("dashboardAdminToken", value);
    }
  };

  const save = () => {
    setStatus("Saving…");
    const headers = { "Content-Type": "application/json" };
    if (adminToken.trim()) {
      headers["X-Dashboard-Admin-Token"] = adminToken.trim();
    }
    fetch(`${API_BASE}/api/settings/upstox`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        access_token: accessToken,
        api_key: apiKey,
        api_secret: apiSecret,
        base_url: baseUrl,
      }),
    })
      .then(async (r) => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          const msg = formatApiErrorDetail(body.detail) || r.statusText || "Save failed";
          throw new Error(msg);
        }
        return body;
      })
      .then((savedBody) => {
        setAccessToken("");
        setApiKey("");
        setApiSecret("");
        const br = savedBody.bot_restart;
        let extra = "";
        if (br?.skipped) {
          extra = ` Bot: ${br.skipped}`;
        } else if (br?.mode === "systemd") {
          extra = br.restarted
            ? ` Bot restarted via systemctl (${br.unit}).`
            : ` Bot systemd restart failed${br.systemctl_message ? `: ${br.systemctl_message}` : ""}.`;
        } else if (br?.mode === "process") {
          extra = br.restarted
            ? " Stopped prior bot process and started a new trading_bot.py."
            : ` Bot recycle incomplete: ${JSON.stringify(br.spawn || br.terminate || br)}`;
        }
        setStatus(`Saved.${extra || " (no bot restart — no fields changed.)"}`);
        loadSettings();
      })
      .catch((e) => {
        setStatus(e.message || String(e));
      });
  };

  return (
    <section className="card upstox-settings">
      <h2>Upstox credentials</h2>
      <p className="upstox-settings-help">
        Daily access token and API key/secret are stored in{" "}
        <code className="inline-code">{credentialsFile || "upstox_credentials.json"}</code> on the
        server (not in source code). Leave a field empty to keep the current value. Saving a
        changed field also restarts the trading bot on the server so a new token applies without SSH.
        {credentialsPath ? (
          <>
            {" "}
            Full path: <code className="inline-code">{credentialsPath}</code>
          </>
        ) : null}
      </p>
      {flags.admin_token_configured ? (
        <label className="upstox-field">
          <span>Dashboard admin token</span>
          <input
            type="password"
            autoComplete="off"
            placeholder="Matches DASHBOARD_ADMIN_TOKEN on server"
            value={adminToken}
            onChange={(e) => persistAdminToken(e.target.value)}
          />
        </label>
      ) : null}
      {loading ? (
        <p className="subtle">Loading…</p>
      ) : (
        <>
          <div className="upstox-previews subtle">
            {flags.has_access_token ? (
              <span>Current access token: {previews.access_token_preview || "set"}</span>
            ) : (
              <span>No access token on file yet</span>
            )}
            {previews.api_key_preview ? (
              <span> · API key: {previews.api_key_preview}</span>
            ) : null}
            {previews.api_secret_preview ? (
              <span> · API secret: {previews.api_secret_preview}</span>
            ) : null}
          </div>
          <label className="upstox-field">
            <span>Base URL</span>
            <input
              type="text"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://api.upstox.com/v2"
            />
          </label>
          <label className="upstox-field">
            <span>Access token</span>
            <input
              type="password"
              autoComplete="off"
              placeholder="Paste new token to replace"
              value={accessToken}
              onChange={(e) => setAccessToken(e.target.value)}
            />
          </label>
          <label className="upstox-field">
            <span>API key</span>
            <input
              type="password"
              autoComplete="off"
              placeholder="Optional — paste to replace"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </label>
          <label className="upstox-field">
            <span>API secret</span>
            <input
              type="password"
              autoComplete="off"
              placeholder="Optional — paste to replace"
              value={apiSecret}
              onChange={(e) => setApiSecret(e.target.value)}
            />
          </label>
          <div className="upstox-actions">
            <button type="button" className="btn-primary" onClick={save}>
              Save to server
            </button>
            <button type="button" className="btn-ghost" onClick={loadSettings}>
              Reload
            </button>
          </div>
          {status ? <p className="upstox-status">{status}</p> : null}
        </>
      )}
    </section>
  );
}
