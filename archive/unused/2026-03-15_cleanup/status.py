import json
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime

STATE_FILE = Path("trading_state.json")
BOT_FILE = Path("trading_bot.py")
ORDER_LOG_FILE = Path("orders.log")

API_BASE_URL = "https://api.upstox.com/v2"
INTERVAL = "1minute"

SCRIPT_TO_INSTRUMENT = {
    "NIFTY": "NSE_FO|51714",
    "BANKNIFTY": "NSE_FO|51701",
    "SENSEX": "BSE_FO|825565",
    "CRUDE": "MCX_FO|472789",
    "GOLDMINI": "MCX_FO|487665",
    "SILVERMINI": "MCX_FO|457533"
}

LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "SENSEX": 20,
    "CRUDE": 100,
    "GOLDMINI": 1,
    "SILVERMINI": 5
}


def _load_realized_pnl_from_orders(log_date_text=None):
    if not ORDER_LOG_FILE.exists():
        return 0.0

    if log_date_text is None:
        log_date_text = datetime.now().strftime("%Y-%m-%d")

    open_positions = {}
    realized = 0.0

    for line in ORDER_LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.startswith(log_date_text):
            continue
        if " | ACTION=ENTRY | " in line:
            parts = line.split(" - ", 1)
            if len(parts) < 2:
                continue
            script = parts[1].split(" | ", 1)[0].strip()
            side_marker = "| SIDE="
            price_marker = "| PRICE="
            side_idx = line.find(side_marker)
            price_idx = line.find(price_marker)
            if side_idx == -1 or price_idx == -1:
                continue
            side = line[side_idx + len(side_marker):].split(" | ", 1)[0].strip()
            price_text = line[price_idx + len(price_marker):].split(" | ", 1)[0].strip()
            try:
                entry_price = float(price_text)
            except Exception:
                continue
            open_positions[script] = (side, entry_price)
            continue

        if " | ACTION=EXIT | " in line:
            parts = line.split(" - ", 1)
            if len(parts) < 2:
                continue
            script = parts[1].split(" | ", 1)[0].strip()
            if script not in open_positions:
                continue
            side_marker = "| SIDE="
            price_marker = "| PRICE="
            side_idx = line.find(side_marker)
            price_idx = line.find(price_marker)
            if side_idx == -1 or price_idx == -1:
                continue
            exit_side = line[side_idx + len(side_marker):].split(" | ", 1)[0].strip()
            price_text = line[price_idx + len(price_marker):].split(" | ", 1)[0].strip()
            try:
                exit_price = float(price_text)
            except Exception:
                continue

            entry_side, entry_price = open_positions.pop(script)
            if entry_side == "BUY" and exit_side == "SELL":
                points = exit_price - entry_price
            elif entry_side == "SELL" and exit_side == "BUY":
                points = entry_price - exit_price
            else:
                continue

            realized += points * LOT_SIZES.get(script, 1)

    return realized


def _extract_access_token():
    if not BOT_FILE.exists():
        return None

    content = BOT_FILE.read_text(encoding="utf-8", errors="ignore")
    marker = '"access_token":'
    idx = content.find(marker)
    if idx == -1:
        return None

    start = content.find('"', idx + len(marker))
    if start == -1:
        return None
    end = content.find('"', start + 1)
    if end == -1:
        return None

    token = content[start + 1:end].strip()
    return token or None


def _fetch_ltp(access_token, instrument_key):
    url = f"{API_BASE_URL}/historical-candle/intraday/{instrument_key}/{INTERVAL}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        payload = response.json()
        candles = payload.get("data", {}).get("candles", [])
        if candles:
            df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp")
            return float(df.iloc[-1]["close"])
    except Exception:
        pass

    quote_url = f"{API_BASE_URL}/market-quote/ltp?instrument_key={instrument_key}"
    response = requests.get(quote_url, headers=headers, timeout=15)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        return None

    instrument_data = payload.get("data", {}).get(instrument_key, {})
    ltp = instrument_data.get("last_price")
    return float(ltp) if ltp is not None else None


def _print_header():
    print("\n" + "=" * 140)
    print(f"{'LIVE POSITION STATUS':<30} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 140)
    header = f"{'Stock':<10} {'Entry':<12} {'SL':<12} {'Target':<12} {'LTP':<12} {'Status':<8} {'Points':<12} {'P&L':<12}"
    print(header)
    print("-" * 140)


def main():
    if not STATE_FILE.exists():
        print("No trading state found. Run trading_bot.py first.")
        return

    access_token = _extract_access_token()
    if not access_token:
        print("Could not read access token from trading_bot.py")
        return

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    positions = state.get("positions", {})

    if not positions:
        print("No active positions right now.")
        return

    _print_header()
    total_open_pnl = 0.0

    for script, pos in positions.items():
        entry = float(pos.get("entry_price", 0.0))
        sl = float(pos.get("stop_loss", entry))
        side = pos.get("type", "-")
        lot_size = LOT_SIZES.get(script, 1)

        target = pos.get("target_price")
        if target is None:
            target_percent = 2.0 / 100
            target = entry * (1 + target_percent) if side == "BUY" else entry * (1 - target_percent)
        target = float(target)

        instrument_key = SCRIPT_TO_INSTRUMENT.get(script)
        ltp = None
        if instrument_key:
            try:
                ltp = _fetch_ltp(access_token, instrument_key)
            except Exception:
                ltp = None

        ltp_text = f"{ltp:.2f}" if ltp is not None else "NA"
        if ltp is not None:
            points = (ltp - entry) if side == "BUY" else (entry - ltp)
            pnl = points * lot_size
            total_open_pnl += pnl
            row = f"{script:<10} {entry:<12.2f} {sl:<12.2f} {target:<12.2f} {ltp_text:<12} {side:<8} {points:<12.2f} {pnl:<12.2f}"
            print(row)
        else:
            row = f"{script:<10} {entry:<12.2f} {sl:<12.2f} {target:<12.2f} {'NA':<12} {side:<8} {'NA':<12} {'NA':<12}"
            print(row)

    print("-" * 140)
    print("=" * 140 + "\n")


if __name__ == "__main__":
    main()
