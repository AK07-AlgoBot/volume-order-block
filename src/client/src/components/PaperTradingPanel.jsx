import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import { ClosedTradesTable } from "./ClosedTradesTable";

function getLocalDateIso() {
  const now = new Date();
  const offsetMs = now.getTimezoneOffset() * 60000;
  return new Date(now.getTime() - offsetMs).toISOString().slice(0, 10);
}

export function PaperTradingPanel({ active }) {
  const [logText, setLogText] = useState("");
  const [logMeta, setLogMeta] = useState({ truncated: false, line_count: 0, path: "" });
  const [logErr, setLogErr] = useState("");
  const [logLoading, setLogLoading] = useState(false);

  const [closedTrades, setClosedTrades] = useState([]);
  const [closedDates, setClosedDates] = useState([]);
  const [selectedDate, setSelectedDate] = useState(getLocalDateIso);
  const [paperTotal, setPaperTotal] = useState(0);
  const [tradesErr, setTradesErr] = useState("");

  const loadLog = useCallback(() => {
    setLogErr("");
    setLogLoading(true);
    apiFetch("/api/logs/paper?max_lines=2000")
      .then((r) => {
        if (r.status === 401) {
          throw new Error("Session expired — sign in again.");
        }
        if (!r.ok) {
          throw new Error("Failed to load paper log");
        }
        return r.json();
      })
      .then((data) => {
        setLogMeta({
          truncated: !!data.truncated,
          line_count: Number(data.line_count || 0),
          path: String(data.path || ""),
        });
        const lines = Array.isArray(data.lines) ? data.lines : [];
        setLogText(lines.join("\n"));
      })
      .catch((e) => {
        setLogErr(e.message || "Error loading log");
      })
      .finally(() => {
        setLogLoading(false);
      });
  }, []);

  const loadTrades = useCallback((date) => {
    setTradesErr("");
    const q = date ? `?date=${encodeURIComponent(date)}` : "";
    apiFetch(`/api/dashboard/paper-closed-trades${q}`)
      .then((r) => {
        if (r.status === 401) {
          throw new Error("Session expired — sign in again.");
        }
        if (!r.ok) {
          throw new Error("Failed to load paper P&L");
        }
        return r.json();
      })
      .then((payload) => {
        setClosedTrades(payload.closed_trades || []);
        setClosedDates(payload.closed_trade_dates || []);
        setPaperTotal(Number(payload.paper_total_realized || 0));
        if (payload.selected_date) {
          setSelectedDate(String(payload.selected_date));
        }
      })
      .catch((e) => {
        setTradesErr(e.message || "Error");
      });
  }, []);

  useEffect(() => {
    if (!active) {
      return undefined;
    }
    loadLog();
    const interval = window.setInterval(loadLog, 30000);
    return () => window.clearInterval(interval);
  }, [active, loadLog]);

  useEffect(() => {
    if (!active) {
      return undefined;
    }
    loadTrades(selectedDate);
    return undefined;
  }, [active, selectedDate, loadTrades]);

  if (!active) {
    return null;
  }

  return (
    <div className="paper-trading-stack">
      <section className="orders-log-section paper-log-section">
        <div className="orders-log-toolbar">
          <div>
            <h2 className="orders-log-title">paper_orders.log</h2>
            <p className="orders-log-sub">
              Virtual trades (no broker orders). Last lines from{" "}
              <code>{logMeta.path || "users/AK07/logs/paper_orders.log"}</code>
              {logMeta.line_count ? ` · ${logMeta.line_count} lines` : null}
              {logMeta.truncated ? " · truncated (file or limit)" : null}
            </p>
          </div>
          <button type="button" className="btn-refresh-log" onClick={loadLog} disabled={logLoading}>
            {logLoading ? "Loading…" : "Refresh"}
          </button>
        </div>
        {logErr ? <div className="login-error orders-log-error">{logErr}</div> : null}
        <pre className="orders-log-pre paper-log-pre" aria-label="Paper orders log contents">
          {logText || (logLoading ? "…" : "— empty or missing —")}
        </pre>
      </section>

      <div className="paper-pnl-banner subtle">
        All-time realized (paper, from log): <strong>{paperTotal.toFixed(2)}</strong>
      </div>
      {tradesErr ? <div className="login-error orders-log-error">{tradesErr}</div> : null}
      <ClosedTradesTable
        title="Paper closed trades (virtual)"
        trades={closedTrades}
        availableDates={closedDates}
        selectedDate={selectedDate}
        onDateChange={setSelectedDate}
      />
    </div>
  );
}
