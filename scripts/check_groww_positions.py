#!/usr/bin/env python3
"""Groww FNO positions + today's orders for a dashboard user (e.g. Nani).

  docker compose -p ak07 -f configs/docker-compose.yml exec -T api \\
    python scripts/check_groww_positions.py Nani
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "server" / "src"))

from app.config.paths import ensure_repo_and_lib_on_path  # noqa: E402

ensure_repo_and_lib_on_path()

from app.services.groww_engine import GrowwClient  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def _print_orders(client: GrowwClient, username: str, today: str) -> None:
    import requests
    from groww_credentials_store import groww_auth_header, read_credentials_file_for_user

    creds = read_credentials_file_for_user(username)
    base = (creds.get("base_url") or "https://api.groww.in").rstrip("/")
    try:
        resp = requests.get(
            f"{base}/v1/order/list",
            params={"segment": "FNO", "page": 0, "page_size": 50},
            headers=groww_auth_header(creds),
            timeout=30,
        )
        payload = resp.json() if resp.text else {}
    except requests.RequestException as exc:
        print(f"Orders API error: {exc}")
        return

    if resp.status_code != 200 or payload.get("status") != "SUCCESS":
        print(f"Orders HTTP {resp.status_code} — {str(payload)[:200]}")
        return

    orders = (payload.get("payload") or {}).get("order_list") or []
    today_rows = [
        row
        for row in orders
        if isinstance(row, dict)
        and (
            today in str(row.get("created_at") or row.get("trade_date") or "")
            or str(row.get("order_reference_id") or "").startswith("ak07s3")
        )
    ]
    print(f"=== Today's FNO orders ({len(today_rows)}) ===")
    if not today_rows:
        print("No FNO orders today — S3 entry likely failed or not triggered yet.")
        return
    for row in today_rows:
        sym = row.get("trading_symbol", "?")
        side = row.get("transaction_type", "?")
        qty = row.get("quantity", "?")
        status = row.get("order_status", "?")
        filled = row.get("filled_quantity", "?")
        avg = row.get("average_fill_price", "?")
        ref = row.get("order_reference_id", "")
        print(
            f"  {side:4} {qty:>3} x {sym:<18} status={status} filled={filled} avg={avg} ref={ref}"
        )


def main() -> int:
    username = (sys.argv[1] if len(sys.argv) > 1 else "Nani").strip()
    today = datetime.now(IST).date().isoformat()
    print(f"=== Groww FNO positions — {username} ({today}) ===\n")

    client = GrowwClient(username)
    if not client.has_token():
        print(f"{username}: no Groww access_token — refresh in Token Update.")
        return 1

    positions = client.get_fno_positions()
    if not positions:
        print("No open FNO positions on Groww (flat).")
    else:
        print(f"Open FNO positions: {len(positions)}\n")
        for row in positions:
            sym = row.get("trading_symbol", "?")
            qty = row.get("quantity", row.get("net_quantity", 0))
            product = row.get("product", "?")
            net_px = row.get("net_price", row.get("net_carry_forward_price", "?"))
            realised = row.get("realised_pnl", 0)
            exchange = row.get("exchange", "?")
            print(f"  {sym:<18} qty={qty:>4}  product={product}  net={net_px}  realised=₹{realised}  ({exchange})")
        print("\nNote: unrealised P&L is on the Groww app; API shows realised_pnl for closed legs.")

    print()
    _print_orders(client, username, today)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
