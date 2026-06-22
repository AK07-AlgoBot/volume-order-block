"""Analyse S7 winning vs losing trade entry times to find the sweet spot."""
import json
import sys
from collections import defaultdict
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "backtest_s7v2_90d.json")
trades = json.loads(path.read_text(encoding="utf-8")).get("trades", [])

LOT = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}

# Bucket by hour+slot
bucket_w = defaultdict(int)
bucket_l = defaultdict(int)
bucket_pts = defaultdict(float)

for t in trades:
    ts = t["entry_at"]  # e.g. 2026-04-01T10:25:00+05:30
    hhmm = ts[11:16]    # "10:25"
    pts = t["pnl_points"]
    if pts > 0:
        bucket_w[hhmm] += 1
    else:
        bucket_l[hhmm] += 1
    bucket_pts[hhmm] += pts

all_slots = sorted(set(list(bucket_w) + list(bucket_l)))

print()
print("Entry-Time Analysis (S7)")
print("-" * 55)
print(f"  {'Time':>6}  {'W':>3} {'L':>3}  {'WR':>6}  {'PnL pts':>9}")
print("-" * 55)

cumulative_pts = 0.0
best_cutoff_pts = 0.0
best_cutoff_time = ""

for slot in all_slots:
    w = bucket_w[slot]
    l = bucket_l[slot]
    n = w + l
    wr = f"{100*w/n:.0f}%" if n else "-"
    pts = bucket_pts[slot]
    cumulative_pts += pts
    print(f"  {slot:>6}  {w:>3} {l:>3}  {wr:>6}  {pts:>+9.2f}   cumul: {cumulative_pts:+.2f}")

print("-" * 55)

# Cumulative WR by entry cutoff time
print()
print("What if we STOP entering after a certain time?")
print("-" * 55)
print(f"  {'Stop at':>7}  {'Trades':>7}  {'W':>4} {'L':>4}  {'WR':>6}  {'Pts':>8}  {'INR ~':>9}")
print("-" * 55)
slots_sorted = sorted(all_slots)
for cutoff_idx in range(len(slots_sorted)):
    cutoff = slots_sorted[cutoff_idx]
    w = sum(bucket_w[s] for s in slots_sorted[:cutoff_idx+1])
    l = sum(bucket_l[s] for s in slots_sorted[:cutoff_idx+1])
    n = w + l
    wr = f"{100*w/n:.0f}%" if n else "-"
    pts = sum(bucket_pts[s] for s in slots_sorted[:cutoff_idx+1])
    inr_est = pts * 30  # rough avg across indices
    print(f"  {cutoff:>7}  {n:>7}  {w:>4} {l:>4}  {wr:>6}  {pts:>+8.2f}  {inr_est:>+9,.0f}")
print("-" * 55)

print()
print("Index + Time breakdown")
print("-" * 55)
by_idx_time = defaultdict(lambda: defaultdict(lambda: {"w": 0, "l": 0, "pts": 0.0}))
for t in trades:
    sym = t["symbol"]
    ts = t["entry_at"]
    hhmm = ts[11:16]
    pts = t["pnl_points"]
    by_idx_time[sym][hhmm]["pts"] += pts
    by_idx_time[sym][hhmm]["w" if pts > 0 else "l"] += 1

for sym in ["NIFTY", "BANKNIFTY", "SENSEX"]:
    print(f"\n  {sym}")
    print(f"  {'Time':>6}  {'W':>3} {'L':>3}  {'PnL':>8}")
    for slot in slots_sorted:
        d = by_idx_time[sym].get(slot)
        if not d:
            continue
        n = d["w"] + d["l"]
        if n == 0:
            continue
        print(f"  {slot:>6}  {d['w']:>3} {d['l']:>3}  {d['pts']:>+8.2f}")
