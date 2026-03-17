function asNumber(value) {
  const numberValue = Number(value ?? 0);
  return Number.isFinite(numberValue) ? numberValue : 0;
}

function formatTimestamp(value) {
  if (!value) return "-";
  const normalized = String(value).replace("T", " ");
  return normalized.length > 19 ? normalized.slice(0, 19) : normalized;
}

function isProfitLocked(trade) {
  const side = String(trade.side || "");
  const entry = asNumber(trade.entry_price);
  const sl = asNumber(trade.stop_loss);
  if (entry <= 0) return false;
  if (side === "BUY") return sl > entry;
  if (side === "SELL") return sl < entry;
  return false;
}

export function LiveTradesTable({ trades }) {
  return (
    <div className="card">
      <h2>Live Trades</h2>
      <div className="subtle">{trades.length} open positions</div>
      <div className="table-wrap">
        <table className="trade-table live-trades-table">
          <colgroup>
            <col className="col-symbol" />
            <col className="col-side" />
            <col className="col-price" />
            <col className="col-price" />
            <col className="col-price" />
            <col className="col-price" />
            <col className="col-chart" />
            <col className="col-win" />
            <col className="col-pnl" />
            <col className="col-datetime" />
          </colgroup>
          <thead>
            <tr>
              <th className="col-left">Symbol</th>
              <th className="col-center">Side</th>
              <th className="col-center-num">Entry</th>
              <th className="col-center-num">LTP</th>
              <th className="col-center-num">Target</th>
              <th className="col-center-num">SL</th>
              <th className="col-center-num">Chart %</th>
              <th className="col-center-num">Win %</th>
              <th className="col-center-num">Unrealized P&L</th>
              <th className="col-left">Opened At</th>
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 ? (
              <tr>
                <td className="empty" colSpan={10}>
                  No live trades
                </td>
              </tr>
            ) : (
              trades.map((trade) => {
                const pnl = asNumber(trade.unrealized_pnl);
                const lockedProfit = isProfitLocked(trade);
                const rawChartPct = trade.chart_percent;
                const chartPct = Number(rawChartPct);
                const hasChartPct =
                  rawChartPct !== null &&
                  rawChartPct !== undefined &&
                  String(rawChartPct).trim() !== "" &&
                  Number.isFinite(chartPct);
                const rawWin = trade.win_percent;
                const win = Number(rawWin);
                const hasWin =
                  rawWin !== null &&
                  rawWin !== undefined &&
                  String(rawWin).trim() !== "" &&
                  Number.isFinite(win);
                return (
                  <tr key={trade.id}>
                    <td className="col-left">{trade.symbol}</td>
                    <td className={`col-center ${trade.side === "BUY" ? "buy" : "sell"}`}>{trade.side}</td>
                    <td className="col-center-num">{asNumber(trade.entry_price).toFixed(2)}</td>
                    <td className="col-center-num">{asNumber(trade.last_price).toFixed(2)}</td>
                    <td className="col-center-num">{asNumber(trade.target_price).toFixed(2)}</td>
                    <td className="col-center-num">
                      <span className="sl-cell">
                        <span>{asNumber(trade.stop_loss).toFixed(2)}</span>
                        {lockedProfit ? <span className="lock-pill">L</span> : null}
                      </span>
                    </td>
                    <td className="col-center-num">{hasChartPct ? `${chartPct.toFixed(2)}%` : "-"}</td>
                    <td className="col-center-num">{hasWin ? `${win.toFixed(1)}%` : "-"}</td>
                    <td className={`col-center-num ${pnl >= 0 ? "pnl-pos" : "pnl-neg"}`}>{pnl.toFixed(2)}</td>
                    <td className="col-left date-cell">{formatTimestamp(trade.opened_at)}</td>
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
