import re
import argparse
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

ORDER_LOG_FILE = Path("orders.log")
BOT_LOG_FILE = Path("trading_bot.log")

LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "SENSEX": 20,
    "CRUDE": 100,
    "GOLDMINI": 1,
    "SILVERMINI": 5,
}

LINE_PATTERN = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - "
    r"(?P<script>[^|]+) \| ACTION=(?P<action>ENTRY|EXIT) \| SIDE=(?P<side>BUY|SELL) "
    r"\| PRICE=(?P<price>\d+(?:\.\d+)?)"
)

BOT_PRICE_PATTERN = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - INFO -\s+"
    r"(?P<script>[A-Z0-9_]+): .*\| Latest: Rs(?P<price>\d+(?:\.\d+)?)"
)


def _parse_ts(ts_str):
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")


def build_bot_price_map(lines):
    script_price_points = defaultdict(list)

    for raw in lines:
        match = BOT_PRICE_PATTERN.search(raw.strip())
        if not match:
            continue

        script = match.group("script").strip()
        ts = _parse_ts(match.group("ts"))
        price = float(match.group("price"))
        script_price_points[script].append((ts, price))

    return script_price_points


def find_nearest_price(script_price_points, script, target_ts, max_seconds=120):
    points = script_price_points.get(script)
    if not points:
        return None

    best = None
    best_diff = None

    for ts, price in points:
        diff = abs((ts - target_ts).total_seconds())
        if diff > max_seconds:
            continue

        if best_diff is None or diff < best_diff:
            best = price
            best_diff = diff

    return best


def parse_orders(lines, script_price_points=None):
    entries = defaultdict(deque)
    closed = []

    for raw in lines:
        match = LINE_PATTERN.search(raw.strip())
        if not match:
            continue

        ts = match.group("ts")
        script = match.group("script").strip()
        action = match.group("action")
        side = match.group("side")
        price = float(match.group("price"))
        raw_line = raw.strip()

        if action == "ENTRY":
            entries[script].append({"ts": ts, "side": side, "price": price})
            continue

        if not entries[script]:
            continue

        entry = entries[script].popleft()
        lot = LOT_SIZES.get(script, 1)

        exit_price = price
        price_source = "order_log"
        if (
            "REASON=EOD_SQUAREOFF" in raw_line
            and abs(price - entry["price"]) < 1e-9
            and script_price_points is not None
        ):
            nearest = find_nearest_price(
                script_price_points, script, _parse_ts(ts), max_seconds=120
            )
            if nearest is not None:
                exit_price = nearest
                price_source = "bot_log_nearest"

        if entry["side"] == "BUY" and side == "SELL":
            points = exit_price - entry["price"]
        elif entry["side"] == "SELL" and side == "BUY":
            points = entry["price"] - exit_price
        else:
            points = 0.0

        pnl = points * lot
        closed.append(
            {
                "script": script,
                "entry_time": entry["ts"],
                "exit_time": ts,
                "entry_side": entry["side"],
                "entry_price": entry["price"],
                "exit_price": exit_price,
                "price_source": price_source,
                "points": points,
                "lot": lot,
                "pnl": pnl,
            }
        )

    open_positions = {
        script: list(queue) for script, queue in entries.items() if queue
    }
    return closed, open_positions


def print_report(closed, open_positions):
    if not closed:
        print("No closed trades found.")
        return

    print("\n" + "=" * 140)
    print("LOG-BASED CLOSED TRADES REPORT")
    print("=" * 140)
    print(
        f"{'Script':<12} {'Entry Time':<23} {'Exit Time':<23} {'Side':<6} {'Entry':<10} {'Exit':<10} {'Points':<10} {'Lot':<6} {'PnL':<12} {'PriceSrc':<14}"
    )
    print("-" * 140)

    total_pnl = 0.0
    win_count = 0

    for trade in closed:
        total_pnl += trade["pnl"]
        if trade["pnl"] > 0:
            win_count += 1

        print(
            f"{trade['script']:<12} {trade['entry_time']:<23} {trade['exit_time']:<23} "
            f"{trade['entry_side']:<6} {trade['entry_price']:<10.2f} {trade['exit_price']:<10.2f} "
            f"{trade['points']:<10.2f} {trade['lot']:<6} {trade['pnl']:<12.2f} {trade.get('price_source', 'order_log'):<14}"
        )

    print("-" * 140)
    total_trades = len(closed)
    loss_count = total_trades - win_count
    win_rate = (win_count / total_trades) * 100 if total_trades else 0.0
    print(f"Total Closed Trades : {total_trades}")
    print(f"Winning Trades      : {win_count}")
    print(f"Losing Trades       : {loss_count}")
    print(f"Win Rate            : {win_rate:.2f}%")
    print(f"Net Realized PnL    : {total_pnl:.2f}")

    if open_positions:
        print("\nOpen Entries Still Not Closed:")
        for script, entries in open_positions.items():
            for entry in entries:
                print(
                    f"- {script}: {entry['side']} @ {entry['price']:.2f} (entry_time={entry['ts']})"
                )

    print("=" * 140 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Generate closed trades report from orders.log")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include all dates from orders.log (default: only today's date).",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Filter by specific date in YYYY-MM-DD format.",
    )
    args = parser.parse_args()

    if not ORDER_LOG_FILE.exists():
        print("orders.log not found.")
        return

    lines = ORDER_LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
    bot_log_lines = []
    if BOT_LOG_FILE.exists():
        bot_log_lines = BOT_LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()

    if args.all:
        filtered_lines = lines
        selected_date = "ALL"
    else:
        selected_date = args.date or datetime.now().strftime("%Y-%m-%d")
        filtered_lines = [line for line in lines if line.startswith(selected_date)]

    print(f"Report Date Filter : {selected_date}")

    if not filtered_lines:
        print("No log entries found for selected date filter.")
        return

    script_price_points = build_bot_price_map(bot_log_lines) if bot_log_lines else None
    closed, open_positions = parse_orders(filtered_lines, script_price_points=script_price_points)
    print_report(closed, open_positions)


if __name__ == "__main__":
    main()
