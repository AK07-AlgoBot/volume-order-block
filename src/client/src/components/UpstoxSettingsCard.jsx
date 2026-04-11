import { useCallback, useEffect, useState } from "react";
import { apiFetch, getStoredAuth } from "../api/client";

const BROKERS = [
  { id: "upstox", label: "Upstox" },
  { id: "zerodha", label: "Zerodha (Kite)" },
];

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

  const [broker, setBroker] = useState("upstox");
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
  const [credentialsFile, setCredentialsFile] = useState("");
  const [credentialSubject, setCredentialSubject] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState(false);

  const loadSettings = useCallback(() => {
    setLoading(true);
    apiFetch(`/api/settings/credentials?broker=${encodeURIComponent(broker)}`)
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
        setCredentialsFile(data.credentials_file || "");
        setCredentialSubject(data.credential_subject || username || "");
        setStatus("");
      })
      .catch((e) => {
        console.error(e);
        setStatus("Could not load settings (is the API running?)");
      })
      .finally(() => setLoading(false));
  }, [username, broker]);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("kite") === "connected") {
      setBroker("zerodha");
      setStatus("Zerodha (Kite) session saved via OAuth. Use Test connection to verify.");
      window.history.replaceState({}, "", window.location.pathname);
    } else if (params.get("kite_error")) {
      setBroker("zerodha");
      const code = params.get("kite_error") || "unknown";
      setStatus(`Kite login did not complete: ${code.replace(/_/g, " ")}`);
      window.history.replaceState({}, "", window.location.pathname);
    }
  }, []);

  const connectZerodhaOAuth = () => {
    setStatus("Opening Zerodha login…");
    apiFetch("/api/auth/kite/oauth/start")
      .then(async (r) => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          const msg = formatApiErrorDetail(body.detail) || r.statusText || "Could not start Kite login";
          throw new Error(msg);
        }
        return body;
      })
      .then((body) => {
        if (body.login_url) {
          window.location.assign(body.login_url);
        } else {
          setStatus("No login_url in response.");
        }
      })
      .catch((e) => {
        setStatus(e.message || String(e));
      });
  };

  const save = () => {
    setStatus("Saving…");
    const body = {
      broker,
      access_token: accessToken,
      api_key: apiKey,
      api_secret: apiSecret,
      base_url: baseUrl,
    };
    apiFetch("/api/settings/credentials", {
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
        setStatus(`Saved ${savedBody.broker || broker} credentials for ${who}.${extra || ""}`);
        loadSettings();
      })
      .catch((e) => {
        setStatus(e.message || String(e));
      });
  };

  const testConnection = () => {
    setTesting(true);
    setStatus(`Testing saved ${broker === "zerodha" ? "Kite" : "Upstox"} token…`);
    apiFetch(`/api/settings/credentials/test?broker=${encodeURIComponent(broker)}`, {
      method: "POST",
    })
      .then(async (r) => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          const msg = formatApiErrorDetail(body.detail) || r.statusText || "Connection test failed";
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
        const label = broker === "zerodha" ? "Kite" : "Upstox";
        setStatus(`Connection OK for ${who}. ${label} profile check succeeded.`);
      })
      .catch((e) => {
        setStatus(e.message || String(e));
      })
      .finally(() => setTesting(false));
  };

  const brokerHelp =
    broker === "zerodha" ? (
      <>
        Zerodha uses Kite Connect. Set <code className="inline-code">KITE_API_KEY</code>,{" "}
        <code className="inline-code">KITE_API_SECRET</code>, and{" "}
        <code className="inline-code">KITE_REDIRECT_URL</code> on the server (repo{" "}
        <code className="inline-code">.env</code>) to match your Kite app — redirect URL must be exactly{" "}
        <code className="inline-code">…/kite/callback</code> on this API. Use{" "}
        <strong>Connect with Zerodha</strong> to log in at Zerodha (OTP as usual); the server exchanges
        the token and saves <code className="inline-code">zerodha_credentials.json</code>. Or paste
        credentials manually. The live trading bot still uses Upstox until Zerodha execution is wired in.
      </>
    ) : (
      <>
        Upstox credentials live in <code className="inline-code">upstox_credentials.json</code>. The
        trading bot reads this file for the AK07 account.
      </>
    );

  return (
    <section className="card upstox-settings">
      <h2>Broker credentials</h2>
      <p className="upstox-settings-help">
        {brokerHelp}
        {credentialsPath ? (
          <>
            {" "}
            Current file: <code className="inline-code">{credentialsFile || credentialsPath}</code>
            <br />
            <span className="subtle">Path: </span>
            <code className="inline-code">{credentialsPath}</code>
          </>
        ) : null}
      </p>
      <p className="subtle">
        Signed in as <strong>{username}</strong>.
      </p>
      <label className="upstox-field">
        <span>Broker</span>
        <select
          value={broker}
          onChange={(e) => setBroker(e.target.value)}
        >
          {BROKERS.map((b) => (
            <option key={b.id} value={b.id}>
              {b.label}
            </option>
          ))}
        </select>
      </label>
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
              placeholder={
                broker === "zerodha" ? "https://api.kite.trade" : "https://api.upstox.com/v2"
              }
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
              placeholder={broker === "zerodha" ? "Kite API key" : "Optional — paste to replace"}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </label>
          <label className="upstox-field">
            <span>API secret</span>
            <input
              type="password"
              autoComplete="off"
              placeholder={broker === "zerodha" ? "Kite API secret" : "Optional — paste to replace"}
              value={apiSecret}
              onChange={(e) => setApiSecret(e.target.value)}
            />
          </label>
          <div className="upstox-actions">
            {broker === "zerodha" ? (
              <button type="button" className="btn-primary" onClick={connectZerodhaOAuth}>
                Connect with Zerodha
              </button>
            ) : null}
            <button
              type="button"
              className={broker === "zerodha" ? "btn-ghost" : "btn-primary"}
              onClick={save}
            >
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
