import re
import argparse
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

ORDER_LOG_FILE = Path("orders.log")

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


def parse_orders(lines):
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

        if action == "ENTRY":
            entries[script].append({"ts": ts, "side": side, "price": price})
            continue

        if not entries[script]:
            continue

        entry = entries[script].popleft()
        lot = LOT_SIZES.get(script, 1)

        if entry["side"] == "BUY" and side == "SELL":
            points = price - entry["price"]
        elif entry["side"] == "SELL" and side == "BUY":
            points = entry["price"] - price
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
                "exit_price": price,
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
        f"{'Script':<12} {'Entry Time':<23} {'Exit Time':<23} {'Side':<6} {'Entry':<10} {'Exit':<10} {'Points':<10} {'Lot':<6} {'PnL':<12}"
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
            f"{trade['points']:<10.2f} {trade['lot']:<6} {trade['pnl']:<12.2f}"
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

    closed, open_positions = parse_orders(filtered_lines)
    print_report(closed, open_positions)


if __name__ == "__main__":
    main()
