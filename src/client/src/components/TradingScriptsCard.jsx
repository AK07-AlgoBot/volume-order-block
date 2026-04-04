import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";

export function TradingScriptsCard() {
  const [available, setAvailable] = useState([]);
  const [tradeAll, setTradeAll] = useState(true);
  const [selected, setSelected] = useState(() => new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");
  const [scopeLabel, setScopeLabel] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    setStatus("");
    apiFetch("/api/settings/trading-scripts")
      .then((r) => {
        if (!r.ok) {
          throw new Error(`HTTP ${r.status}`);
        }
        return r.json();
      })
      .then((data) => {
        const avail = data.available_scripts || [];
        setAvailable(avail);
        const ens = data.enabled_scripts;
        const all = ens == null;
        setTradeAll(all);
        if (all) {
          setSelected(new Set(avail));
        } else {
          setSelected(new Set(ens || []));
        }
        setScopeLabel(data.mode === "subset" ? "Custom subset" : "All symbols");
      })
      .catch(() => {
        setStatus("Could not load trading scope.");
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const toggleSymbol = (sym) => {
    if (tradeAll) {
      return;
    }
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(sym)) {
        next.delete(sym);
      } else {
        next.add(sym);
      }
      return next;
    });
  };

  const onSave = async () => {
    setSaving(true);
    setStatus("");
    try {
      const payload = {
        enabled_scripts: tradeAll ? null : Array.from(selected),
      };
      if (!tradeAll && selected.size === 0) {
        setStatus("Pick at least one symbol, or use “Trade all symbols”.");
        setSaving(false);
        return;
      }
      const r = await apiFetch("/api/settings/trading-scripts", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        const detail = err.detail || r.statusText;
        setStatus(typeof detail === "string" ? detail : "Save failed.");
        return;
      }
      setStatus("Saved. Bot reads this each loop — no restart needed.");
      setScopeLabel(tradeAll ? "All symbols" : "Custom subset");
    } catch {
      setStatus("Save failed.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="upstox-settings trading-scope-card">
      <div className="upstox-settings-help">
        <h2 className="section-title">Symbols to trade (today)</h2>
        <p>
          The bot only requests market data and opens new trades for the symbols you enable. Open
          positions on other symbols are still managed until closed.
        </p>
        {scopeLabel ? (
          <p className="subtle">
            Current: <strong>{scopeLabel}</strong>
            {loading ? " · loading…" : null}
          </p>
        ) : null}
      </div>

      <label className="trading-scope-all">
        <input
          type="checkbox"
          checked={tradeAll}
          onChange={(e) => {
            const on = e.target.checked;
            setTradeAll(on);
            if (on) {
              setSelected(new Set(available));
            }
          }}
        />
        Trade all symbols
      </label>

      <div className={`trading-scope-grid ${tradeAll ? "trading-scope-grid--disabled" : ""}`}>
        {available.map((sym) => (
          <label key={sym} className="trading-scope-chip">
            <input
              type="checkbox"
              checked={tradeAll || selected.has(sym)}
              disabled={tradeAll}
              onChange={() => toggleSymbol(sym)}
            />
            {sym}
          </label>
        ))}
      </div>

      <div className="upstox-actions">
        <button type="button" className="btn-primary" disabled={saving || loading} onClick={onSave}>
          {saving ? "Saving…" : "Save scope"}
        </button>
        <button type="button" className="btn-ghost" disabled={loading} onClick={load}>
          Reload
        </button>
      </div>
      {status ? <p className="upstox-status">{status}</p> : null}
    </section>
  );
}
