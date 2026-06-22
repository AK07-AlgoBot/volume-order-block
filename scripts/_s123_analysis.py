"""Analyse S1/S2/S3 backtest results."""
import json
import statistics
from datetime import datetime

data = json.load(open("backtest_s123_v3.json"))
trades = data["trades"]

for strat_key in ["Strategy 1", "Strategy 2", "Strategy 3"]:
    st = [t for t in trades if t["strategy"].startswith(strat_key)]
    if not st:
        continue
    wins = [t for t in st if t["pnl_points"] > 0.01]
    losses = [t for t in st if t["pnl_points"] < -0.01]
    avg_win = sum(t["pnl_points"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_points"] for t in losses) / len(losses) if losses else 0
    rr = abs(avg_win / avg_loss) if avg_loss else 0
    total = sum(t["pnl_points"] for t in st)
    print(f"{strat_key}: {len(st)} trades | {len(wins)}W/{len(losses)}L ({100*len(wins)/(len(wins)+len(losses)):.0f}% WR) | avg_win={avg_win:.1f} avg_loss={avg_loss:.1f} | RR={rr:.2f} | total={total:+.0f}")
    by_symbol = {}
    for t in st:
        by_symbol.setdefault(t["symbol"], []).append(t["pnl_points"])
    for sym, pts in sorted(by_symbol.items()):
        sw = sum(1 for p in pts if p > 0.01)
        sl = sum(1 for p in pts if p < -0.01)
        wr = f"{100*sw/(sw+sl):.0f}%" if (sw+sl) > 0 else "--"
        print(f"  {sym}: {len(pts)} trades {sw}W/{sl}L ({wr}) pnl={sum(pts):+.0f}")
    print()

# S3 detailed breakdown
print("=== S3 Per-Index Time Breakdown ===")
s3 = [t for t in trades if t["strategy"].startswith("Strategy 3")]
for idx in ["NIFTY", "BANKNIFTY"]:
    idx_t = [t for t in s3 if t["symbol"] == idx]
    wins = [t for t in idx_t if t["pnl_points"] > 0.01]
    losses = [t for t in idx_t if t["pnl_points"] < -0.01]
    loss_pts = [-t["pnl_points"] for t in losses]
    win_pts = [t["pnl_points"] for t in wins]
    print(f"\n{idx}: {len(wins)}W/{len(losses)}L")
    if loss_pts:
        print(f"  Losses: min={min(loss_pts):.0f} median={statistics.median(loss_pts):.0f} max={max(loss_pts):.0f}")
    if win_pts:
        print(f"  Wins:   min={min(win_pts):.0f} median={statistics.median(win_pts):.0f} max={max(win_pts):.0f}")
    time_data = {}
    for t in idx_t:
        try:
            hour = datetime.fromisoformat(t["entry_at"]).strftime("%H:%M")
            time_data.setdefault(hour, []).append(t["pnl_points"])
        except Exception:
            pass
    for h in sorted(time_data.keys()):
        pts = time_data[h]
        w = sum(1 for p in pts if p > 0.01)
        l = sum(1 for p in pts if p < -0.01)
        wr = f"{100*w/(w+l):.0f}%" if (w+l) > 0 else "--"
        print(f"  {h}: {w}W/{l}L ({wr}) pnl={sum(pts):+.0f}")
