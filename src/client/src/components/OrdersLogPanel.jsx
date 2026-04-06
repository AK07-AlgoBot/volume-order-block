import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";

export function OrdersLogPanel({ active }) {
  const [text, setText] = useState("");
  const [meta, setMeta] = useState({ truncated: false, line_count: 0, path: "" });
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    setErr("");
    setLoading(true);
    apiFetch("/api/logs/orders?max_lines=2000")
      .then((r) => {
        if (r.status === 401) {
          throw new Error("Session expired — sign in again.");
        }
        if (!r.ok) {
          throw new Error("Failed to load orders log");
        }
        return r.json();
      })
      .then((data) => {
        setMeta({
          truncated: !!data.truncated,
          line_count: Number(data.line_count || 0),
          path: String(data.path || ""),
        });
        const lines = Array.isArray(data.lines) ? data.lines : [];
        setText(lines.join("\n"));
      })
      .catch((e) => {
        setErr(e.message || "Error loading log");
      })
      .finally(() => {
        setLoading(false);
      });
  }, []);

  useEffect(() => {
    if (!active) {
      return undefined;
    }
    load();
    const interval = window.setInterval(load, 30000);
    return () => window.clearInterval(interval);
  }, [active, load]);

  if (!active) {
    return null;
  }

  return (
    <section className="orders-log-section">
      <div className="orders-log-toolbar">
        <div>
          <h2 className="orders-log-title">orders.log</h2>
          <p className="orders-log-sub">
            Last lines from <code>{meta.path || "users/AK07/logs/orders.log"}</code>
            {meta.line_count ? ` · ${meta.line_count} lines` : null}
            {meta.truncated ? " · truncated (file or limit)" : null}
          </p>
        </div>
        <button type="button" className="btn-refresh-log" onClick={load} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>
      {err ? <div className="login-error orders-log-error">{err}</div> : null}
      <pre className="orders-log-pre" aria-label="Orders log contents">
        {text || (loading ? "…" : "— empty or missing —")}
      </pre>
    </section>
  );
}
