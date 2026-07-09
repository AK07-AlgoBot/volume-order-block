#!/usr/bin/env python3
"""Today's Groww FNO orders for a dashboard user (e.g. Nani).

  docker compose -p ak07 -f configs/docker-compose.yml exec -T api \
    python scripts/check_groww_orders.py Nani
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "server" / "src"))

from app.config.paths import ensure_repo_and_lib_on_path  # noqa: E402

ensure_repo_and_lib_on_path()

from groww_credentials_store import groww_auth_header, read_credentials_file_for_user  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def main() -> int:
    username = (sys.argv[1] if len(sys.argv) > 1 else "Nani").strip()
    creds = read_credentials_file_for_user(username)
    token = (creds.get("access_token") or "").strip()
    base = (creds.get("base_url") or "https://api.groww.in").rstrip("/")
    if not token:
        print(f"{username}: no Groww access_token")
        return 1

    today = datetime.now(IST).date().isoformat()
    print(f"=== Groww FNO orders today ({today}) — {username} ===\n")

    url = f"{base}/v1/order/list"
    params = {"segment": "FNO", "page": 0, "page_size": 100}
    try:
        resp = requests.get(url, params=params, headers=groww_auth_header(creds), timeout=30)
        payload = resp.json() if resp.text else {}
    except requests.RequestException as exc:
        print(f"API error: {exc}")
        return 1

    if resp.status_code != 200 or payload.get("status") != "SUCCESS":
        print(f"HTTP {resp.status_code} — {str(payload)[:300]}")
        return 1

    orders = (payload.get("payload") or {}).get("order_list") or []
    if not isinstance(orders, list):
        orders = []

    # Prefer today's orders; show ak07-s3 refs first
    today_orders = []
    other = []
    for row in orders:
        if not isinstance(row, dict):
            continue
        ref = str(row.get("order_reference_id") or "")
        created = str(row.get("created_at") or row.get("trade_date") or "")
        if today in created or ref.startswith("ak07s3") or ref.startswith("ak07-s3"):
            today_orders.append(row)
        else:
            other.append(row)

    rows = today_orders or orders[:20]
    if not rows:
        print("No FNO orders returned from Groww today.")
        print("Also check breakout engine logs for S3 placement attempts.")
        return 0

    for row in rows:
        sym = row.get("trading_symbol", "?")
        side = row.get("transaction_type", "?")
        qty = row.get("quantity", "?")
        status = row.get("order_status", "?")
        filled = row.get("filled_quantity", "?")
        avg = row.get("average_fill_price", "?")
        oid = row.get("groww_order_id", "?")
        ref = row.get("order_reference_id", "")
        remark = str(row.get("remark") or "")
        created = row.get("created_at") or row.get("exchange_time") or ""
        print(
            f"{created}  {side:4}  {qty:>3} x {sym:<18}  "
            f"status={status}  filled={filled}  avg={avg}  id={oid}"
        )
        if ref:
            print(f"           ref={ref}")
        if remark and status.upper() in ("REJECTED", "FAILED", "CANCELLED"):
            print(f"           reason={remark[:200]}")

    s3 = [
        r
        for r in rows
        if str(r.get("order_reference_id") or "").startswith(("ak07s3", "ak07-s3"))
    ]
    if s3:
        print(f"\nOK — {len(s3)} AK07 S3 order(s) found on Groww.")
    elif today_orders:
        print(f"\n{len(today_orders)} order(s) today — none tagged ak07-s3 (may be manual).")
    else:
        print("\nNo orders tagged ak07-s3 today — S3 may not have triggered yet.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
