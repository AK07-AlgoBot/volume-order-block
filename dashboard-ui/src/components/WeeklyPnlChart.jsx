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

export function WeeklyPnlChart({
  points,
  weekTotal,
  selectedWeekOffset,
  weekOptions,
  onWeekChange,
}) {
  const pnlValues = (points || []).map((point) => Number(point?.pnl ?? 0));
  const maxGain = pnlValues.length ? Math.max(...pnlValues) : 0;
  const maxLoss = pnlValues.length ? Math.min(...pnlValues) : 0;
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
            Max gain: <span style={{ color: "#4ade80", fontWeight: 700 }}>{maxGainText}</span>
          </div>
          <div>
            Max loss: <span style={{ color: "#fb7185", fontWeight: 700 }}>{maxLossText}</span>
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
      <div className="subtle">Daily realized P&L</div>
      <div style={{ width: "100%", height: 320 }}>
        <ResponsiveContainer>
          <AreaChart data={points}>
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
              contentStyle={{
                backgroundColor: "#020617",
                border: "1px solid #334155",
                color: "#e2e8f0",
              }}
            />
            <ReferenceLine y={0} stroke="#64748b" strokeDasharray="4 4" />
            <Area type="monotone" dataKey="pnl" stroke="#22c55e" fill="url(#pnlFill)" />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
