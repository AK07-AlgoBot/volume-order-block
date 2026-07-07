#!/usr/bin/env python3
"""Verify S3 resolves the nearest NIFTY index future (same path as breakout_engine).

  cd ~/volume-order-block
  docker compose -p ak07 -f configs/docker-compose.yml exec -T api python scripts/check_s3_nifty_future.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "server" / "src"))

from app.config.paths import ensure_repo_and_lib_on_path  # noqa: E402

ensure_repo_and_lib_on_path()

from app.services.upstox_engine import (  # noqa: E402
    INDEX_CONFIGS,
    _future_expiry_key,
    _index_futures_from_master,
    _row_is_index_future,
    build_upstox_client,
)


def _list_nifty_futures(client) -> list[dict]:
    today = date.today().isoformat()
    merged: list[dict] = []
    seen: set[str] = set()
    for params in (
        {"query": "NIFTY", "exchanges": "NSE", "segments": "FO", "expiry": "current_month", "records": 30},
        {"query": "NIFTY", "exchanges": "NSE", "segments": "FO", "expiry": "near_month", "records": 30},
        {"query": "NIFTY", "exchanges": "NSE", "segments": "FO", "records": 30},
    ):
        data = client._get(f"{client.base_url}/instruments/search", params)  # noqa: SLF001
        if not isinstance(data, list):
            continue
        for row in data:
            key = str(row.get("instrument_key") or "")
            if key and key not in seen:
                seen.add(key)
                merged.append(row)
    if not merged:
        merged = _index_futures_from_master("NIFTY")
    rows = [row for row in merged if _row_is_index_future(row, "NIFTY")]
    rows = [row for row in rows if _future_expiry_key(row)[:10] >= today]
    rows.sort(key=_future_expiry_key)
    return rows


def main() -> int:
    cfg = INDEX_CONFIGS["NIFTY"]
    client = build_upstox_client()
    picked = client.get_index_future_contract("NIFTY")
    all_futs = _list_nifty_futures(client)

    print("=== S3 NIFTY future resolution ===\n")
    print(f"Index: {cfg.display} ({cfg.code})")
    print(f"Spot key: {cfg.spot_instrument_key}")
    print(f"Lot size (env/default): {cfg.lot_size}\n")

    spot = client.get_ltp(cfg.spot_instrument_key)
    print(f"Nifty spot LTP: {spot if spot is not None else 'unavailable'}\n")

    print("All live NIFTY index futures (NSE_FO, expiry >= today):")
    if not all_futs:
        print("  (none returned — check Upstox token or deploy latest resolver fix)")
    for i, row in enumerate(all_futs):
        exp = _future_expiry_key(row)[:10]
        sym = row.get("trading_symbol")
        key = row.get("instrument_key")
        lot = row.get("lot_size")
        inst = row.get("instrument_type")
        mark = "  <-- S3 picks this (nearest expiry)" if i == 0 else ""
        print(f"  {exp}  {sym}  type={inst}  lot={lot}  key={key}{mark}")

    print()
    if not picked:
        print("S3 resolve_future: FAILED — no contract")
        return 1

    print("S3 would trade:")
    print(f"  contract_label: {picked.get('contract_label')}")
    print(f"  trading_symbol: {picked.get('trading_symbol')}")
    print(f"  expiry:         {picked.get('expiry')}")
    print(f"  instrument_key: {picked.get('instrument_key')}")

    fut_ltp = client.get_ltp(str(picked.get("instrument_key") or ""))
    if fut_ltp is not None:
        print(f"  future LTP:     {fut_ltp}")
        if spot is not None:
            print(f"  basis (fut-spot): {fut_ltp - spot:+.2f} pts")

    if all_futs and picked.get("instrument_key") == all_futs[0].get("instrument_key"):
        print("\nOK — S3 picks the nearest-expiry NIFTY future (front month).")
        return 0

    if picked.get("instrument_key"):
        print("\nOK — S3 resolved a NIFTY future (verify expiry vs list above).")
        return 0

    print("\nWARN — could not confirm picked contract against candidate list.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
