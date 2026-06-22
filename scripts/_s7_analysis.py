"""Detailed S7 backtest analysis -- index-wise breakdown with INR PnL.
Usage: python scripts/_s7_analysis.py [json_file]
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "backtest_s7v2_90d.json")
r = json.loads(path.read_text(encoding="utf-8"))
trades = r.get("trades", [])

LOT_SIZE = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}

def inr(pts, symbol):
    return pts * LOT_SIZE.get(symbol, 65)

by_idx = {sym: {"n": 0, "w": 0, "l": 0, "be": 0,
                "pts": 0.0, "wins": [], "losses": [], "inr": 0.0}
          for sym in ["NIFTY", "BANKNIFTY", "SENSEX"]}

monthly = defaultdict(lambda: defaultdict(lambda: {"n": 0, "pts": 0.0, "inr": 0.0}))

for t in trades:
    sym = t["symbol"]
    pts = t["pnl_points"]
    iv = inr(pts, sym)
    b = by_idx.get(sym)
    if not b:
        continue
    b["n"] += 1; b["pts"] += pts; b["inr"] += iv
    month = t["entry_at"][:7]
    monthly[sym][month]["n"] += 1
    monthly[sym][month]["pts"] += pts
    monthly[sym][month]["inr"] += iv
    if pts > 0.01:
        b["w"] += 1; b["wins"].append(pts)
    elif pts < -0.01:
        b["l"] += 1; b["losses"].append(pts)
    else:
        b["be"] += 1

def pct(a, total): return f"{100*a/total:.1f}%" if total else "-"
def avg(lst): return sum(lst)/len(lst) if lst else 0.0

LINE = "-" * 76
total_n = sum(b["n"] for b in by_idx.values())
total_w = sum(b["w"] for b in by_idx.values())
total_l = sum(b["l"] for b in by_idx.values())
total_pts = sum(b["pts"] for b in by_idx.values())
total_inr = sum(b["inr"] for b in by_idx.values())

summ = r.get("summary", {})
start = r.get("start", "?"); end = r.get("end", "?")

print()
print("=" * 76)
print(f"  S7 ORB+ Backtest  {start} -> {end}")
print(f"  {path.name}  |  Lot sizes: Nifty 65 | BankNifty 30 | Sensex 20")
print("=" * 76)
print(f"\n  Overall | {total_n} trades | {total_w}W/{total_l}L | "
      f"WR {pct(total_w,total_n)} | {total_pts:+.2f} pts | INR {total_inr:+,.0f}")

print()
print(LINE)
print(f"  {'INDEX':<12} {'N':>5} {'W':>4} {'L':>4} {'BE':>3} {'WR':>6} "
      f"{'TOTAL PTS':>10} {'AVG WIN':>8} {'AVG LOSS':>9} {'INR PNL':>11}")
print(LINE)
for sym in ["NIFTY", "BANKNIFTY", "SENSEX"]:
    b = by_idx[sym]
    n, w, l = b["n"], b["w"], b["l"]
    print(f"  {sym:<12} {n:>5} {w:>4} {l:>4} {b['be']:>3} "
          f"{pct(w,n):>6} {b['pts']:>+10.2f} {avg(b['wins']):>+8.2f} "
          f"{avg(b['losses']):>+9.2f} {b['inr']:>+11,.0f}")
print(LINE)
print(f"  {'TOTAL':<12} {total_n:>5} {total_w:>4} {total_l:>4} {'':>3} "
      f"{pct(total_w,total_n):>6} {total_pts:>+10.2f} {'':>8} {'':>9} {total_inr:>+11,.0f}")
print(LINE)

TRADING_DAYS = 62
print(f"\n  Daily avg expectancy ({TRADING_DAYS} trading days)")
print(LINE)
for sym in ["NIFTY", "BANKNIFTY", "SENSEX"]:
    b = by_idx[sym]
    d_pts = b["pts"] / TRADING_DAYS
    d_inr = b["inr"] / TRADING_DAYS
    print(f"  {sym:<12}  {d_pts:>+7.2f} pts/day  INR {d_inr:>+7,.0f}/day  "
          f"({b['n']} trades/{TRADING_DAYS}d = {b['n']/TRADING_DAYS:.2f}/day)")
print(LINE)
combined = total_inr / TRADING_DAYS
print(f"\n  Combined daily avg (all 3 indices, 1 lot each): INR {combined:>+,.0f}/day")

if combined > 0:
    mult = 3000 / combined
    print(f"  To reach INR 3,000/day: need ~{mult:.1f}x more lots or ~{mult:.1f}x capital allocation")
    print(f"  e.g. {max(1,round(mult))} lots per trade = INR {combined*max(1,round(mult)):>+,.0f}/day estimate")

# Monthly breakdown
print()
print("  Monthly PnL (pts / INR)")
print(LINE)
months = sorted({m for sym in monthly for m in monthly[sym]})
header = " ".join(f"{m:>16}" for m in months)
print(f"  {'INDEX':<12} {header}")
print(LINE)
for sym in ["NIFTY", "BANKNIFTY", "SENSEX"]:
    row = ""
    for m in months:
        d = monthly[sym].get(m, {"pts": 0.0, "inr": 0.0, "n": 0})
        cell = f"{d['pts']:+.1f}/{d['inr']:+,.0f}" if d["n"] > 0 else "-"
        row += f"  {cell:>14}"
    print(f"  {sym:<12}{row}")
print(LINE)
print()
