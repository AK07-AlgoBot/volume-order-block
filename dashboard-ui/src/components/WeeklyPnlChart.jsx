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

function buildWeeklyChartData(points) {
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

export function WeeklyPnlChart({
  points,
  weekTotal,
  selectedWeekOffset,
  weekOptions,
  onWeekChange,
}) {
  const chartData = buildWeeklyChartData(points);
  const dailyValues = chartData.map((row) => row.daily);
  const maxGain = dailyValues.length ? Math.max(...dailyValues) : 0;
  const maxLoss = dailyValues.length ? Math.min(...dailyValues) : 0;
  const maxGainText = maxGain.toFixed(2);
  const maxLossText = maxLoss.toFixed(2);
  const total = Number(weekTotal || 0);
  const selectedMeta = (weekOptions || []).find(
    (option) => Number(option.week_offset) === Number(selectedWeekOffset)
  );
  const rangeText = selectedMeta
    ? `${selectedMeta.range_start} to ${selectedMeta.range_end}`
    : "Selected week";

  return (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "0.75rem" }}>
        <div>
          <h2 style={{ margin: 0 }}>Weekly P&L</h2>
          <div className="subtle">{rangeText}</div>
        </div>
        <div className="subtle" style={{ textAlign: "right", lineHeight: 1.5 }}>
          <div>
            Week total:{" "}
            <span className={total >= 0 ? "pnl-pos" : "pnl-neg"} style={{ fontWeight: 700 }}>
              {total.toFixed(2)}
            </span>
          </div>
          <div>
            Best day: <span style={{ color: "#4ade80", fontWeight: 700 }}>{maxGainText}</span>
          </div>
          <div>
            Worst day: <span style={{ color: "#fb7185", fontWeight: 700 }}>{maxLossText}</span>
          </div>
        </div>
      </div>
      <div className="weekly-filter-row">
        <label className="subtle" htmlFor="weekly-filter-select">
          Week
        </label>
        <select
          id="weekly-filter-select"
          className="weekly-select"
          value={String(selectedWeekOffset ?? 0)}
          onChange={(event) => onWeekChange?.(Number(event.target.value))}
        >
          {(weekOptions || []).map((option) => (
            <option key={option.week_offset} value={String(option.week_offset)}>
              {option.label} ({option.range_start} to {option.range_end})
            </option>
          ))}
        </select>
      </div>
      <div className="subtle">
        Cumulative realized P&L (running sum Mon→Fri; matches week total at last day)
      </div>
      <div style={{ width: "100%", height: 320 }}>
        <ResponsiveContainer>
          <AreaChart data={chartData}>
            <defs>
              <linearGradient id="pnlFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#22c55e" stopOpacity={0.7} />
                <stop offset="95%" stopColor="#22c55e" stopOpacity={0.05} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
            <XAxis dataKey="date" stroke="#94a3b8" />
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
                    <div>Week so far: {Number(row.cumulative).toFixed(2)}</div>
                  </div>
                );
              }}
            />
            <ReferenceLine y={0} stroke="#64748b" strokeDasharray="4 4" />
            <Area type="monotone" dataKey="cumulative" stroke="#22c55e" fill="url(#pnlFill)" />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
