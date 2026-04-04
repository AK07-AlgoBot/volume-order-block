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

export function UpstoxSettingsCard() {
  const { username } = getStoredAuth();

  const [baseUrl, setBaseUrl] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [previews, setPreviews] = useState({
    access_token_preview: "",
    api_key_preview: "",
    api_secret_preview: "",
  });
  const [flags, setFlags] = useState({
    has_access_token: false,
  });
  const [credentialsPath, setCredentialsPath] = useState("");
  const [credentialSubject, setCredentialSubject] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState(false);

  const loadSettings = useCallback(() => {
    setLoading(true);
    apiFetch("/api/settings/upstox")
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
        });
        setCredentialsPath(data.credentials_path || "");
        setCredentialSubject(data.credential_subject || username || "");
        setStatus("");
      })
      .catch((e) => {
        console.error(e);
        setStatus("Could not load settings (is the API running?)");
      })
      .finally(() => setLoading(false));
  }, [username]);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  const save = () => {
    setStatus("Saving…");
    const body = {
      access_token: accessToken,
      api_key: apiKey,
      api_secret: apiSecret,
      base_url: baseUrl,
    };
    apiFetch("/api/settings/upstox", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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

  const testConnection = () => {
    setTesting(true);
    setStatus("Testing saved Upstox token…");
    apiFetch("/api/settings/upstox/test", { method: "POST" })
      .then(async (r) => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          const msg = formatApiErrorDetail(body.detail) || r.statusText || "Upstox test failed";
          throw new Error(msg);
        }
        return body;
      })
      .then((body) => {
        const profile = body.profile || {};
        const who =
          profile.user_name ||
          profile.email ||
          profile.user_id ||
          body.credential_subject ||
          credentialSubject;
        setStatus(`Connection OK for ${who}. Saved token can access Upstox profile successfully.`);
      })
      .catch((e) => {
        setStatus(e.message || String(e));
      })
      .finally(() => setTesting(false));
  };

  return (
    <section className="card upstox-settings">
      <h2>Upstox credentials</h2>
      <p className="upstox-settings-help">
        Credentials are stored on the server at{" "}
        <code className="inline-code">src/server/data/users/AK07/upstox_credentials.json</code>. The trading bot
        uses this file for the AK07 account.
        {credentialsPath ? (
          <>
            {" "}
            Current file: <code className="inline-code">{credentialsPath}</code>
          </>
        ) : null}
      </p>
      <p className="subtle">
        Signed in as <strong>{username}</strong>.
      </p>
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
            <button type="button" className="btn-ghost" onClick={testConnection} disabled={testing}>
              {testing ? "Testing…" : "Test connection"}
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
