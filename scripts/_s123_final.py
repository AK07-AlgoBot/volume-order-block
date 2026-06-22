"""Final comparison report: baseline vs refined strategies."""
import json

data = json.load(open("backtest_s123_final.json"))
trades = data["trades"]

baseline = {
    "S1": (145, 55, 82, 2238.68),
    "S2": (11, 6, 5, 57.05),
    "S3": (237, 131, 52, -2657.34),
}

total_before = sum(b[3] for b in baseline.values())
total_after = 0.0

print("=" * 65)
print("STRATEGY REFINEMENT RESULTS  (90-day backtest)")
print("=" * 65)
print()

for strat_key, bkey in [("Strategy 1", "S1"), ("Strategy 2", "S2"), ("Strategy 3", "S3")]:
    st = [t for t in trades if t["strategy"].startswith(strat_key)]
    wins = [t for t in st if t["pnl_points"] > 0.01]
    losses = [t for t in st if t["pnl_points"] < -0.01]
    avg_win = sum(t["pnl_points"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_points"] for t in losses) / len(losses) if losses else 0
    rr = abs(avg_win / avg_loss) if avg_loss else 0
    total = sum(t["pnl_points"] for t in st)
    total_after += total
    wr = 100 * len(wins) / (len(wins) + len(losses)) if (wins or losses) else 0
    bt, bw, bl, bpnl = baseline[bkey]
    bwr = 100 * bw / (bw + bl)
    imp = total - bpnl

    print(f"{bkey} | BEFORE: {bt} trades  {bwr:.0f}% WR  {bpnl:+.0f} pts")
    print(f"   | AFTER:  {len(st)} trades  {wr:.0f}% WR  {total:+.0f} pts  (delta: {imp:+.0f})")
    print(f"   | avg_win={avg_win:.0f} pts  avg_loss={avg_loss:.0f} pts  R:R={rr:.2f}:1")
    by_symbol = {}
    for t in st:
        by_symbol.setdefault(t["symbol"], []).append(t["pnl_points"])
    for sym, pts in sorted(by_symbol.items()):
        sw = sum(1 for p in pts if p > 0.01)
        sl = sum(1 for p in pts if p < -0.01)
        wr2 = 100 * sw / (sw + sl) if (sw + sl) > 0 else 0
        print(f"       {sym}: {len(pts)} trades  {sw}W/{sl}L  ({wr2:.0f}% WR)  pnl={sum(pts):+.0f}")
    print()

print("=" * 65)
print(f"COMBINED  BEFORE: {total_before:+.0f} pts")
print(f"COMBINED  AFTER:  {total_after:+.0f} pts")
print(f"IMPROVEMENT:      {total_after - total_before:+.0f} pts  over 90 days")
print(f"Per trading day:  ~{(total_after - total_before) / 62:.0f} pts/day improvement")
print("=" * 65)
