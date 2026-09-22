#!/usr/bin/env python3
"""Map an OF Trap S/B print onto today's ITM option 5m candle.

  python scripts/test_oftrap_option_plan.py --spot 23385.8 --time 10:50 --kind S
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "server" / "src"))
sys.path.insert(0, str(REPO / "src" / "lib"))
os.environ.setdefault("OFTRAP_LIVE", "0")

from app.services.orderflow_trap_oms import OfTrapOms, apply_rr_trail  # noqa: E402


def _selfcheck_trail() -> None:
    sl, armed = apply_rr_trail(entry=100.0, sl=90.0, peak=109.0, r_pts=10.0, target=140.0)
    assert not armed and sl == 90.0
    sl, armed = apply_rr_trail(entry=100.0, sl=90.0, peak=110.0, r_pts=10.0, target=140.0)
    assert armed and sl == 100.0
    sl, armed = apply_rr_trail(entry=100.0, sl=100.0, peak=113.0, r_pts=10.0, target=140.0)
    assert armed and sl == 103.0
    print("trail self-check ok (1R to cost, then +1/pt)")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="NIFTY")
    p.add_argument("--kind", default="S", choices=("S", "B"))
    p.add_argument("--time", default="10:50", help="index absorption 5m start HH:MM")
    p.add_argument("--spot", type=float, default=23385.8)
    args = p.parse_args()
    _selfcheck_trail()
    oms = OfTrapOms()
    plan = oms.plan_from_absorption(args.symbol, args.kind, args.time, args.spot)
    o = plan["option_ohlc"]
    print()
    print(f"{plan['kind']} {plan['symbol']} {plan['bar_time']}  spot {plan['spot']}")
    print(f"ITM {plan['option_side']} {plan['strike']}  {plan['instrument_key']}  exp {plan['expiry']}")
    print(f"opt {plan['bar_time']}  O {o['open']:.2f}  H {o['high']:.2f}  L {o['low']:.2f}  C {o['close']:.2f}")
    print(f"BUY  entry {plan['entry']:.2f}  SL {plan['sl']:.2f}  R {plan['r_pts']:.2f}")
    print(f"TP   {plan['target']:.2f}  (1:{plan['rr']:.0f})")
    print("trail: +1R -> SL to cost, then +1 SL per +1 premium peak")


if __name__ == "__main__":
    main()
