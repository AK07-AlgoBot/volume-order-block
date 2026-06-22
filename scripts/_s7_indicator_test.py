"""Test each new indicator (EMA, Supertrend, ADX, RSI) in isolation.

Usage: python scripts/_s7_indicator_test.py

Temporarily patches the engine env vars to toggle each indicator on/off.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]

def run_test(label: str, env_overrides: dict[str, str]) -> dict:
    env = {**os.environ, **env_overrides}
    result = subprocess.run(
        [sys.executable, str(BASE / "scripts/backtest_ak07.py"),
         "--days", "90", "--strategies", "s7", "--json-out", f"_test_{label}.json"],
        capture_output=True, text=True, env=env, cwd=str(BASE),
    )
    try:
        data = json.loads(Path(BASE / f"_test_{label}.json").read_text())
        s = data.get("summary", {})
        n = s.get("total_trades", 0)
        w = s.get("wins", 0)
        wr = s.get("win_rate_pct", 0.0)
        pts = s.get("total_pts", 0.0)
        return {"label": label, "trades": n, "wins": w, "wr": wr, "pts": pts}
    except Exception as e:
        return {"label": label, "trades": 0, "wins": 0, "wr": 0, "pts": 0, "err": str(e)}

tests = [
    ("v6_baseline",  {}),  # current 75% WR baseline
    ("ema_only",     {"S7_USE_EMA": "1",  "S7_USE_ST": "0",  "S7_USE_ADX": "0", "S7_USE_RSI": "0"}),
    ("st_only",      {"S7_USE_EMA": "0",  "S7_USE_ST": "1",  "S7_USE_ADX": "0", "S7_USE_RSI": "0"}),
    ("adx_only",     {"S7_USE_EMA": "0",  "S7_USE_ST": "0",  "S7_USE_ADX": "1", "S7_USE_RSI": "0"}),
    ("rsi_only",     {"S7_USE_EMA": "0",  "S7_USE_ST": "0",  "S7_USE_ADX": "0", "S7_USE_RSI": "1"}),
    ("ema_st",       {"S7_USE_EMA": "1",  "S7_USE_ST": "1",  "S7_USE_ADX": "0", "S7_USE_RSI": "0"}),
    ("all_4",        {"S7_USE_EMA": "1",  "S7_USE_ST": "1",  "S7_USE_ADX": "1", "S7_USE_RSI": "1"}),
]

# Patch engine to use env vars for optional gate toggling
# (Note: the engine uses the toggles if we set S7_USE_* env vars to control each gate)
print()
print("=" * 65)
print("  Indicator isolation test  (S7 v6 base: 11:20-11:50 window)")
print("=" * 65)
print(f"  {'Config':<16} {'Trades':>7} {'WR':>7} {'Pts':>9}  Notes")
print("-" * 65)

results = []
for label, env in tests:
    r = run_test(label, env)
    results.append(r)
    err = r.get("err", "")
    note = err[:25] if err else ""
    print(f"  {r['label']:<16} {r['trades']:>7} {r['wr']:>6.1f}% {r['pts']:>+9.2f}  {note}")
print("-" * 65)
print()
