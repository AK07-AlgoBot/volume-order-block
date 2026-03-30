import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

function buildCumulativeChartData(points) {
  const sorted = [...(points || [])].sort((a, b) =>
    String(a?.date || "").localeCompare(String(b?.date || ""))
  );
  let cumulative = 0;
  return sorted.map((point) => {
    const daily = Number(point?.pnl ?? 0);
    cumulative += daily;
    return {
      date: point.date,
      daily,
      cumulative: Number(cumulative.toFixed(2)),
    };
  });
}

/**
 * @param {{ total: number, range_start: string, range_end: string }} istMonth — current IST calendar month MTD from API
 */
export function WeeklyPnlChart({
  chartMode,
  onChartModeChange,
  istMonth,
  weekPoints,
  weekTotal,
  weekOptions,
  weekOffset,
  onWeekChange,
  monthPoints,
  monthTotal,
  monthOptions,
  monthOffset,
  onMonthChange,
}) {
  const isWeek = chartMode === "week";
  const points = isWeek ? weekPoints : monthPoints;
  const total = Number((isWeek ? weekTotal : monthTotal) || 0);
  const chartData = buildCumulativeChartData(points);
  const dailyValues = chartData.map((row) => row.daily);
  const maxGain = dailyValues.length ? Math.max(...dailyValues) : 0;
  const maxLoss = dailyValues.length ? Math.min(...dailyValues) : 0;

  const weekMeta = (weekOptions || []).find(
    (o) => Number(o.week_offset) === Number(weekOffset)
  );
  const monthMeta = (monthOptions || []).find(
    (o) => Number(o.month_offset) === Number(monthOffset)
  );
  const rangeText = isWeek
    ? weekMeta
      ? `${weekMeta.range_start} to ${weekMeta.range_end}`
      : "Selected week"
    : monthMeta
      ? `${monthMeta.range_start} to ${monthMeta.range_end}`
      : "Selected month";

  const mtd = istMonth || {};
  const mtdTotal = Number(mtd.total ?? 0);

  return (
    <div className="card">
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          gap: "0.75rem",
          flexWrap: "wrap",
        }}
      >
        <div>
          <h2 style={{ margin: 0 }}>P&amp;L</h2>
          <div className="subtle">{rangeText}</div>
          <div className="ist-month-line">
            Month to date (IST):{" "}
            <span className={mtdTotal >= 0 ? "pnl-pos" : "pnl-neg"} style={{ fontWeight: 700 }}>
              {mtdTotal.toFixed(2)}
            </span>
            {mtd.range_start && mtd.range_end ? (
              <span className="subtle">
                {" "}
                · {mtd.range_start} to {mtd.range_end}
              </span>
            ) : null}
          </div>
        </div>
        <div className="subtle" style={{ textAlign: "right", lineHeight: 1.5 }}>
          <div>
            {isWeek ? "Week" : "Month"} total:{" "}
            <span className={total >= 0 ? "pnl-pos" : "pnl-neg"} style={{ fontWeight: 700 }}>
              {total.toFixed(2)}
            </span>
          </div>
          <div>
            Best day: <span style={{ color: "#4ade80", fontWeight: 700 }}>{maxGain.toFixed(2)}</span>
          </div>
          <div>
            Worst day: <span style={{ color: "#fb7185", fontWeight: 700 }}>{maxLoss.toFixed(2)}</span>
          </div>
        </div>
      </div>

      <div className="pnl-mode-row">
        <span className="subtle">Chart</span>
        <div className="pnl-mode-toggle" role="group" aria-label="Chart period">
          <button
            type="button"
            className={isWeek ? "active" : ""}
            onClick={() => onChartModeChange?.("week")}
          >
            Week
          </button>
          <button
            type="button"
            className={!isWeek ? "active" : ""}
            onClick={() => onChartModeChange?.("month")}
          >
            Month
          </button>
        </div>
      </div>

      <div className="weekly-filter-row">
        <label className="subtle" htmlFor="pnl-period-select">
          {isWeek ? "Week" : "Month"}
        </label>
        <select
          id="pnl-period-select"
          className="weekly-select"
          value={String(isWeek ? weekOffset ?? 0 : monthOffset ?? 0)}
          onChange={(event) => {
            const v = Number(event.target.value);
            if (isWeek) {
              onWeekChange?.(v);
            } else {
              onMonthChange?.(v);
            }
          }}
        >
          {isWeek
            ? (weekOptions || []).map((option) => (
                <option key={option.week_offset} value={String(option.week_offset)}>
                  {option.label} ({option.range_start} to {option.range_end})
                </option>
              ))
            : (monthOptions || []).map((option) => (
                <option key={option.month_offset} value={String(option.month_offset)}>
                  {option.label} ({option.range_start} to {option.range_end})
                </option>
              ))}
        </select>
      </div>
      <div className="subtle">
        {isWeek
          ? "Cumulative realized P&L (running sum Mon→Fri; week total matches last day)"
          : "Cumulative realized P&L over the selected calendar month (IST)"}
      </div>
      <div style={{ width: "100%", height: 320 }}>
        <ResponsiveContainer>
          <AreaChart data={chartData}>
            <defs>
              <linearGradient id="pnlFillUnified" x1="0" y1="0" x2="0" y2="1">
                <stop
                  offset="5%"
                  stopColor={isWeek ? "#22c55e" : "#38bdf8"}
                  stopOpacity={0.7}
                />
                <stop
                  offset="95%"
                  stopColor={isWeek ? "#22c55e" : "#38bdf8"}
                  stopOpacity={0.05}
                />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
            <XAxis dataKey="date" stroke="#94a3b8" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
            <YAxis stroke="#94a3b8" />
            <Tooltip
              content={({ active, payload, label }) => {
                if (!active || !payload?.length) {
                  return null;
                }
                const row = payload[0]?.payload;
                if (!row) {
                  return null;
                }
                return (
                  <div
                    style={{
                      backgroundColor: "#020617",
                      border: "1px solid #334155",
                      color: "#e2e8f0",
                      padding: "0.5rem 0.65rem",
                      fontSize: "0.8rem",
                    }}
                  >
                    <div style={{ marginBottom: "0.35rem", fontWeight: 600 }}>{label}</div>
                    <div>That day: {Number(row.daily).toFixed(2)}</div>
                    <div>
                      {isWeek ? "Week" : "Month"} so far: {Number(row.cumulative).toFixed(2)}
                    </div>
                  </div>
                );
              }}
            />
            <ReferenceLine y={0} stroke="#64748b" strokeDasharray="4 4" />
            <Area
              type="monotone"
              dataKey="cumulative"
              stroke={isWeek ? "#22c55e" : "#38bdf8"}
              fill="url(#pnlFillUnified)"
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
