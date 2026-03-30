import { useCallback, useEffect, useState } from "react";
import { apiFetch, getStoredAuth } from "../api/client";

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

function settingsUrl(forUser) {
  const base = "/api/settings/upstox";
  if (forUser) {
    return `${base}?for_user=${encodeURIComponent(forUser)}`;
  }
  return base;
}

export function UpstoxSettingsCard() {
  const { role, username } = getStoredAuth();
  const isAdmin = role === "admin";

  /** Admin: whose Upstox file we are editing (empty = own account). */
  const [credentialTarget, setCredentialTarget] = useState("");
  const [userOptions, setUserOptions] = useState([]);

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
  const [credentialSubject, setCredentialSubject] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!isAdmin) {
      return;
    }
    apiFetch("/api/auth/users")
      .then((r) => r.json())
      .then((rows) => setUserOptions(Array.isArray(rows) ? rows : []))
      .catch(() => setUserOptions([]));
  }, [isAdmin]);

  const effectiveForUser = isAdmin && credentialTarget ? credentialTarget : "";

  const loadSettings = useCallback(() => {
    setLoading(true);
    apiFetch(settingsUrl(effectiveForUser))
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
        setCredentialSubject(data.credential_subject || username || "");
        setStatus("");
      })
      .catch((e) => {
        console.error(e);
        setStatus("Could not load settings (is the API running on port 8000?)");
      })
      .finally(() => setLoading(false));
  }, [effectiveForUser, username]);

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
    const body = {
      access_token: accessToken,
      api_key: apiKey,
      api_secret: apiSecret,
      base_url: baseUrl,
    };
    if (isAdmin && credentialTarget) {
      body.for_user = credentialTarget;
    }
    apiFetch("/api/settings/upstox", {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    })
      .then(async (r) => {
        const saved = await r.json().catch(() => ({}));
        if (!r.ok) {
          const msg = formatApiErrorDetail(saved.detail) || r.statusText || "Save failed";
          throw new Error(msg);
        }
        return saved;
      })
      .then((savedBody) => {
        setAccessToken("");
        setApiKey("");
        setApiSecret("");
        const br = savedBody.bot_restart;
        const who = savedBody.credential_subject || credentialSubject;
        let extra = "";
        if (br?.skipped) {
          extra = ` Bot: ${br.skipped}`;
        } else if (br?.mode === "systemd") {
          extra = br.restarted
            ? ` Bot restarted via systemctl (${br.unit}).`
            : ` Bot systemd restart failed${br.systemctl_message ? `: ${br.systemctl_message}` : ""}.`;
        } else if (br?.mode === "process") {
          extra = br.restarted
            ? " Bot process recycled."
            : ` Bot recycle incomplete: ${JSON.stringify(br.spawn || br.terminate || br)}`;
        }
        setStatus(`Saved for ${who}.${extra || " (no bot restart — no fields changed.)"}`);
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
        Each dashboard user has a separate file on the server:{" "}
        <code className="inline-code">server/data/users/&lt;user&gt;/upstox_credentials.json</code>.
        Sign in as that user (or as admin and pick an account below) to paste tokens. The trading bot loads
        every user who has a saved access token and trades in one process.
        {credentialsPath ? (
          <>
            {" "}
            Current file: <code className="inline-code">{credentialsPath}</code>
          </>
        ) : null}
      </p>
      {isAdmin ? (
        <label className="upstox-field">
          <span>Save / edit credentials for</span>
          <select
            className="admin-view-select"
            value={credentialTarget}
            onChange={(e) => setCredentialTarget(e.target.value)}
            style={{ maxWidth: "100%" }}
          >
            <option value="">My admin account ({username})</option>
            {userOptions.map((u) => (
              <option key={u.username} value={u.username}>
                {u.username} ({u.role})
              </option>
            ))}
          </select>
        </label>
      ) : null}
      {!isAdmin ? (
        <p className="subtle">
          Signed in as <strong>{username}</strong>. Only you (and admins) can change these credentials.
        </p>
      ) : null}
      {flags.admin_token_configured ? (
        <label className="upstox-field">
          <span>Dashboard admin token (legacy)</span>
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
          <p className="subtle">
            Editing: <strong>{credentialSubject}</strong>
          </p>
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
