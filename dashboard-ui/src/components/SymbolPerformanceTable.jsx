function asNumber(value) {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? n : 0;
}

export function SymbolPerformanceTable({
  rows = [],
  periodDays = 14,
  onPeriodChange,
  tradeCount = 0,
  cutoffDate = "",
  endDate = "",
}) {
  return (
    <div className="card symbol-perf-card">
      <div className="closed-header">
        <h2>Symbol performance</h2>
        <div className="closed-filter">
          <label htmlFor="perf-days-select" className="subtle">
            Window
          </label>
          <select
            id="perf-days-select"
            className="closed-date-select"
            value={String(periodDays)}
            onChange={(e) => onPeriodChange?.(Number(e.target.value))}
          >
            <option value="7">Last 7 days</option>
            <option value="14">Last 14 days</option>
            <option value="30">Last 30 days</option>
            <option value="90">Last 90 days</option>
          </select>
        </div>
      </div>
      <div className="subtle">
        {tradeCount} closed trades from {cutoffDate || "—"} to {endDate || "—"} (by close date)
      </div>
      <div className="table-wrap closed-table-wrap">
        <table className="trade-table symbol-perf-table">
          <thead>
            <tr>
              <th className="col-left">Symbol</th>
              <th className="col-center-num">Trades</th>
              <th className="col-center-num">W / L</th>
              <th className="col-center-num">Win %</th>
              <th className="col-center-num">Total P&amp;L</th>
              <th className="col-center-num">Avg P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={6} className="subtle col-left">
                  No closed trades in this window.
                </td>
              </tr>
            ) : (
              rows.map((row) => {
                const total = asNumber(row.total_pnl);
                const cls = total > 0 ? "pnl-pos" : total < 0 ? "pnl-neg" : "";
                return (
                  <tr key={row.symbol}>
                    <td className="col-left">{row.symbol}</td>
                    <td className="col-center-num">{row.trades}</td>
                    <td className="col-center-num">
                      {row.wins} / {row.losses}
                      {row.breakeven ? ` (${row.breakeven} flat)` : ""}
                    </td>
                    <td className="col-center-num">{asNumber(row.win_rate_pct).toFixed(1)}%</td>
                    <td className={`col-center-num ${cls}`}>{total.toFixed(2)}</td>
                    <td className="col-center-num">{asNumber(row.avg_pnl).toFixed(2)}</td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
