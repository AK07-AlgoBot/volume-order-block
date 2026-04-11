import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../api/client";

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

function fmtNum(x) {
  if (x == null || Number.isNaN(Number(x))) {
    return "—";
  }
  return Number(x).toFixed(2);
}

function SectionBlock({ title, data }) {
  if (!data) {
    return null;
  }
  return (
    <div className="order-block-section">
      <h3 className="order-block-section-title">{title}</h3>
      {data.timeframes ? (
        <p className="subtle order-block-meta">{data.timeframes}</p>
      ) : null}
      <dl className="order-block-dl">
        <dt>Direction</dt>
        <dd>{data.direction || "—"}</dd>
        <dt>Entry probability</dt>
        <dd>{data.entry_probability != null ? `${data.entry_probability}%` : "—"}</dd>
        <dt>Entry (ref.)</dt>
        <dd>{fmtNum(data.entry)}</dd>
        <dt>Stop loss (ref.)</dt>
        <dd>{fmtNum(data.stop_loss)}</dd>
        <dt>Target (ref.)</dt>
        <dd>{fmtNum(data.target)}</dd>
        <dt>Structural swing high</dt>
        <dd>{fmtNum(data.structural_swing_high)}</dd>
        <dt>Structural swing low</dt>
        <dd>{fmtNum(data.structural_swing_low)}</dd>
      </dl>
      {data.invalidation ? (
        <p className="order-block-warning subtle">{data.invalidation}</p>
      ) : null}
      {data.notes?.length ? (
        <ul className="order-block-notes">
          {data.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function OrderBlockCard() {
  const [symbol, setSymbol] = useState("");
  const [suggestions, setSuggestions] = useState([]);
  const [open, setOpen] = useState(false);
  const [searchBusy, setSearchBusy] = useState(false);
  const [searchHint, setSearchHint] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);
  const blurCloseTimer = useRef(null);

  useEffect(() => {
    const q = symbol.trim();
    if (q.length < 2) {
      setSuggestions([]);
      setSearchHint("");
      return undefined;
    }
    const t = window.setTimeout(() => {
      setSearchBusy(true);
      apiFetch(`/api/market/instruments/search?q=${encodeURIComponent(q)}&limit=30`)
        .then((r) => r.json().then((body) => ({ ok: r.ok, body })))
        .then(({ ok, body }) => {
          if (!ok) {
            setSuggestions([]);
            setSearchHint(formatApiErrorDetail(body.detail) || "Search failed");
            return;
          }
          setSuggestions(Array.isArray(body.results) ? body.results : []);
          setSearchHint(
            body.meta?.warning ||
              (body.meta?.cache_age_hours != null
                ? `Instrument list age ~${body.meta.cache_age_hours}h (${body.meta.source || "cache"})`
                : ""),
          );
          setOpen(true);
        })
        .catch(() => {
          setSuggestions([]);
          setSearchHint("");
        })
        .finally(() => setSearchBusy(false));
    }, 320);
    return () => window.clearTimeout(t);
  }, [symbol]);

  const pickSuggestion = (instrumentKey) => {
    setSymbol(instrumentKey);
    setOpen(false);
    setSuggestions([]);
  };

  const onBlurWrap = () => {
    blurCloseTimer.current = window.setTimeout(() => setOpen(false), 200);
  };

  const submit = () => {
    const s = symbol.trim();
    if (!s) {
      setError("Enter or pick a symbol.");
      return;
    }
    setLoading(true);
    setError("");
    setResult(null);
    apiFetch("/api/market/order-block", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: s }),
    })
      .then(async (r) => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          throw new Error(formatApiErrorDetail(body.detail) || r.statusText || "Request failed");
        }
        return body;
      })
      .then((body) => {
        setResult(body);
      })
      .catch((e) => {
        setError(e.message || String(e));
      })
      .finally(() => setLoading(false));
  };

  const analysis = result?.analysis;
  const trend = analysis?.trend;

  return (
    <section className="card order-block-card">
      <h2>Order block (Kite)</h2>
      <p className="upstox-settings-help">
        Type at least <strong>2 characters</strong> to search NSE/BSE equity names (e.g. &quot;ICICI&quot;,
        &quot;bank&quot;). Pick a row or paste an exact key like <code className="inline-code">NSE:ICICIBANK-EQ</code>.
        Analysis uses Zerodha historical candles (intraday: 30m + 5m; positional: 60m + 15m).
      </p>
      <div className="order-block-symbol-wrap" onBlur={onBlurWrap}>
        <label className="upstox-field">
          <span>Symbol</span>
          <div className="order-block-input-row">
            <input
              type="text"
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              placeholder="Search company or symbol…"
              autoComplete="off"
              aria-autocomplete="list"
              aria-expanded={open}
              onFocus={() => {
                if (blurCloseTimer.current) {
                  window.clearTimeout(blurCloseTimer.current);
                }
                if (symbol.trim().length >= 2 && suggestions.length) {
                  setOpen(true);
                }
              }}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  setOpen(false);
                }
              }}
            />
            {searchBusy ? <span className="order-block-search-spinner subtle">Searching…</span> : null}
          </div>
        </label>
        {searchHint ? <p className="subtle order-block-search-hint">{searchHint}</p> : null}
        {open && suggestions.length > 0 ? (
          <ul className="order-block-ac-list" role="listbox">
            {suggestions.map((s) => (
              <li key={s.instrument_key}>
                <button
                  type="button"
                  className="order-block-ac-item"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => pickSuggestion(s.instrument_key)}
                >
                  <span className="order-block-ac-key">{s.instrument_key}</span>
                  <span className="order-block-ac-name">{s.name || "—"}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      <div className="upstox-actions">
        <button type="button" className="btn-primary" onClick={submit} disabled={loading}>
          {loading ? "Analyzing…" : "Analyze"}
        </button>
      </div>
      {error ? <p className="upstox-status">{error}</p> : null}

      {analysis ? (
        <>
          <p className="subtle order-block-instrument">
            {analysis.instrument_key}
            {analysis.instrument_token != null ? (
              <span> · token {analysis.instrument_token}</span>
            ) : null}
          </p>

          {trend?.one_week_and_one_day ? (
            <div className="order-block-section">
              <h3 className="order-block-section-title">Trend (1W + 1D)</h3>
              <dl className="order-block-dl">
                <dt>Weekly bias</dt>
                <dd>{trend.one_week_and_one_day.weekly_bias}</dd>
                <dt>Daily bias</dt>
                <dd>{trend.one_week_and_one_day.daily_bias}</dd>
              </dl>
              <p className="subtle">{trend.one_week_and_one_day.summary}</p>
            </div>
          ) : null}

          <SectionBlock title="Intraday (30m + 5m)" data={analysis.intraday} />
          <SectionBlock title="Positional (~2 weeks, 60m + 15m)" data={analysis.positional} />
        </>
      ) : null}
    </section>
  );
}
