"""S3 deep analysis: extension from breakout level, per-index."""
import json
from datetime import datetime

data = json.load(open("backtest_s123_v2.json"))
trades = data["trades"]
s3 = [t for t in trades if t["strategy"].startswith("Strategy 3")]

for idx in ["NIFTY", "BANKNIFTY", "SENSEX"]:
    idx_t = [t for t in s3 if t["symbol"] == idx]
    wins = [t for t in idx_t if t["pnl_points"] > 0.01]
    losses = [t for t in idx_t if t["pnl_points"] < -0.01]

    # actual loss sizes
    loss_pts = sorted([-t["pnl_points"] for t in losses])
    win_pts = sorted([t["pnl_points"] for t in wins])

    print(f"{idx}: {len(wins)}W/{len(losses)}L  total={sum(t['pnl_points'] for t in idx_t):+.0f}")
    if loss_pts:
        import statistics
        print(f"  losses: min={min(loss_pts):.0f}  median={statistics.median(loss_pts):.0f}  max={max(loss_pts):.0f}")
        print(f"    <80={sum(1 for x in loss_pts if x<80)}  80-120={sum(1 for x in loss_pts if 80<=x<120)}  120-200={sum(1 for x in loss_pts if 120<=x<200)}  >200={sum(1 for x in loss_pts if x>=200)}")
    if win_pts:
        print(f"  wins: min={min(win_pts):.0f}  median={statistics.median(win_pts):.0f}  max={max(win_pts):.0f}")

    # Time breakdown for this index
    time_data = {}
    for t in idx_t:
        try:
            hour = datetime.fromisoformat(t["entry_at"]).strftime("%H:%M")
            time_data.setdefault(hour, []).append(t["pnl_points"])
        except Exception:
            pass
    print("  Time breakdown:")
    for h in sorted(time_data.keys()):
        pts = time_data[h]
        w = sum(1 for p in pts if p > 0.01)
        l = sum(1 for p in pts if p < -0.01)
        wr = f"{100*w/(w+l):.0f}%" if (w+l) > 0 else "--"
        total = sum(pts)
        print(f"    {h}: {w}W/{l}L ({wr}) pnl={total:+.0f}")
    print()
