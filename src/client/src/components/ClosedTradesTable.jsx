function asNumber(value) {
  const numberValue = Number(value ?? 0);
  return Number.isFinite(numberValue) ? numberValue : 0;
}

function formatTimestamp(value) {
  if (!value) return "-";
  const normalized = String(value).replace("T", " ");
  return normalized.length > 19 ? normalized.slice(0, 19) : normalized;
}

export function ClosedTradesTable({
  trades,
  availableDates = [],
  selectedDate = "",
  onDateChange,
  title = "Closed Trades",
}) {
  const dateOptions = availableDates.length > 0 ? availableDates : selectedDate ? [selectedDate] : [];

  return (
    <div className="card">
      <div className="closed-header">
        <h2>{title}</h2>
        <div className="closed-filter">
          <label htmlFor="closed-date-select" className="subtle">
            Date
          </label>
          <select
            id="closed-date-select"
            className="closed-date-select"
            value={selectedDate}
            onChange={(event) => onDateChange?.(event.target.value)}
          >
            {dateOptions.map((dateValue) => (
              <option key={dateValue} value={dateValue}>
                {dateValue}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="subtle">{trades.length} completed positions</div>
      <div className="table-wrap closed-table-wrap">
        <table className="trade-table closed-trades-table">
          <colgroup>
            <col className="col-symbol" />
            <col className="col-side" />
            <col className="col-qty" />
            <col className="col-price" />
            <col className="col-price" />
            <col className="col-pnl" />
            <col className="col-datetime" />
            <col className="col-datetime" />
          </colgroup>
          <thead>
            <tr>
              <th className="col-left">Symbol</th>
              <th className="col-center">Side</th>
              <th className="col-center-num">Qty</th>
              <th className="col-center-num">Entry</th>
              <th className="col-center-num">Exit</th>
              <th className="col-center-num">Realized P&L</th>
              <th className="col-left">Opened At</th>
              <th className="col-left">Closed At</th>
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 ? (
              <tr>
                <td className="empty" colSpan={8}>
                  No closed trades yet
                </td>
              </tr>
            ) : (
              trades.map((trade) => {
                const pnl = asNumber(trade.realized_pnl);
                return (
                  <tr key={trade.id}>
                    <td className="col-left">{trade.symbol}</td>
                    <td className={`col-center ${trade.side === "BUY" ? "buy" : "sell"}`}>{trade.side}</td>
                    <td className="col-center-num">{asNumber(trade.quantity).toFixed(2)}</td>
                    <td className="col-center-num">{asNumber(trade.entry_price).toFixed(2)}</td>
                    <td className="col-center-num">{asNumber(trade.exit_price).toFixed(2)}</td>
                    <td className={`col-center-num ${pnl >= 0 ? "pnl-pos" : "pnl-neg"}`}>{pnl.toFixed(2)}</td>
                    <td className="col-left date-cell">{formatTimestamp(trade.opened_at)}</td>
                    <td className="col-left date-cell">{formatTimestamp(trade.closed_at)}</td>
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
