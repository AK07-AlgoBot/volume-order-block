function asNumber(value) {
  const numberValue = Number(value ?? 0);
  return Number.isFinite(numberValue) ? numberValue : 0;
}

export function LiveTradesTable({ trades }) {
  return (
    <div className="card">
      <h2>Live Trades</h2>
      <div className="subtle">{trades.length} open positions</div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Entry</th>
              <th>Last</th>
              <th>Unrealized P&L</th>
              <th>Opened At</th>
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 ? (
              <tr>
                <td className="empty" colSpan={7}>
                  No live trades
                </td>
              </tr>
            ) : (
              trades.map((trade) => {
                const pnl = asNumber(trade.unrealized_pnl);
                return (
                  <tr key={trade.id}>
                    <td>{trade.symbol}</td>
                    <td className={trade.side === "BUY" ? "buy" : "sell"}>{trade.side}</td>
                    <td>{asNumber(trade.quantity).toFixed(2)}</td>
                    <td>{asNumber(trade.entry_price).toFixed(2)}</td>
                    <td>{asNumber(trade.last_price).toFixed(2)}</td>
                    <td className={pnl >= 0 ? "pnl-pos" : "pnl-neg"}>{pnl.toFixed(2)}</td>
                    <td>{trade.opened_at || "-"}</td>
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
