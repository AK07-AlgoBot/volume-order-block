"""
Scan per-user orders.log + users/<user>/archive/*/logs/orders.log +
legacy users/<holder>/archive/<user>/*/logs/orders.log for closed-trade patterns.

Reconstructs trades with the same LIFO opposite-side pairing as the dashboard,
attaches EXIT REASON and ENTRY chart_pct/chart_vol when present in log lines.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LINE_PATTERN = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - "
    r"(?P<script>[^|]+) \| ACTION=(?P<action>ENTRY|EXIT) \| SIDE=(?P<side>BUY|SELL) "
    r"\| PRICE=(?P<price>\d+(?:\.\d+)?)"
)
ORDER_ID_PATTERN = re.compile(r"order_id=(?P<order_id>\d+)")
REASON_PATTERN = re.compile(r"REASON=(?P<reason>[^|]+)")
CHART_PCT_PATTERN = re.compile(r"chart_pct=(?P<v>[\d.]+)")
CHART_VOL_PATTERN = re.compile(r"chart_vol=(?P<v>[\d.]+)")

LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "SENSEX": 20,
    "CRUDE": 100,
    "GOLDMINI": 1,
    "SILVERMINI": 5,
}


def _parse_ts(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")


def _get_order_log_files(user: str) -> list[Path]:
    from upstox_credentials_store import (
        legacy_admin_bucket_order_logs,
        sanitize_username,
        user_archive_order_logs,
        user_data_dir,
    )

    ud = user_data_dir(sanitize_username(user))
    files: list[Path] = []
    main = ud / "logs" / "orders.log"
    if main.exists():
        files.append(main)
    files.extend(user_archive_order_logs(user))
    files.extend(legacy_admin_bucket_order_logs(user))
    seen: set[str] = set()
    out: list[Path] = []
    for p in files:
        r = str(p.resolve())
        if r in seen:
            continue
        seen.add(r)
        out.append(p)
    return out


def _pop_matching_entry(entry_queue: deque, exit_side: str) -> dict | None:
    expected = "BUY" if exit_side == "SELL" else "SELL"
    for idx in range(len(entry_queue) - 1, -1, -1):
        entry = entry_queue[idx]
        if str(entry.get("side")) == expected:
            if idx == 0:
                return entry_queue.popleft()
            if idx == len(entry_queue) - 1:
                return entry_queue.pop()
            selected = entry
            entry_queue.remove(selected)
            return selected
    return None


def load_events(user: str) -> list[tuple]:
    parsed: list[tuple] = []
    for order_file in _get_order_log_files(user):
        text = order_file.read_text(encoding="utf-8", errors="ignore")
        for raw in text.splitlines():
            stripped = raw.strip()
            m = LINE_PATTERN.search(stripped)
            if not m:
                continue
            ts = _parse_ts(m.group("ts"))
            script = m.group("script").strip()
            action = m.group("action")
            side = m.group("side")
            price = float(m.group("price"))
            oid_m = ORDER_ID_PATTERN.search(stripped)
            order_id = oid_m.group("order_id") if oid_m else ""
            reason = None
            if action == "EXIT":
                rm = REASON_PATTERN.search(stripped)
                if rm:
                    reason = rm.group("reason").strip()
            chart_pct = None
            chart_vol = None
            if action == "ENTRY":
                cm = CHART_PCT_PATTERN.search(stripped)
                if cm:
                    chart_pct = float(cm.group("v"))
                vm = CHART_VOL_PATTERN.search(stripped)
                if vm:
                    chart_vol = float(vm.group("v"))
            parsed.append((ts, script, action, side, price, order_id, reason, chart_pct, chart_vol))

    parsed.sort(key=lambda e: e[0])
    seen: set[tuple] = set()
    deduped: list[tuple] = []
    for e in parsed:
        key = (
            e[0].isoformat(timespec="milliseconds"),
            e[1],
            e[2],
            e[3],
            round(e[4], 4),
            e[5],
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped


def reconstruct_trades(events: list[tuple]) -> list[dict]:
    entries: dict[str, deque] = defaultdict(deque)
    trades: list[dict] = []
    seq = 0
    for ts, script, action, side, price, _oid, reason, chart_pct, chart_vol in events:
        if action == "ENTRY":
            entries[script].append(
                {
                    "side": side,
                    "price": price,
                    "ts": ts,
                    "chart_pct": chart_pct,
                    "chart_volume": chart_vol,
                }
            )
            continue
        if not entries[script]:
            continue
        entry = _pop_matching_entry(entries[script], side)
        if entry is None:
            continue
        lot = float(LOT_SIZES.get(script, 1))
        if entry["side"] == "BUY" and side == "SELL":
            realized = (price - entry["price"]) * lot
        elif entry["side"] == "SELL" and side == "BUY":
            realized = (entry["price"] - price) * lot
        else:
            realized = 0.0
        seq += 1
        trades.append(
            {
                "symbol": script,
                "side": entry["side"],
                "quantity": lot,
                "entry_price": float(entry["price"]),
                "exit_price": float(price),
                "realized_pnl": round(realized, 2),
                "opened_at": entry["ts"],
                "closed_at": ts,
                "exit_reason": reason or "UNKNOWN",
                "chart_pct": entry.get("chart_pct"),
                "chart_volume": entry.get("chart_volume"),
            }
        )
    return trades


def bucket_chart_pct(v: float | None) -> str:
    if v is None:
        return "unknown"
    if v < 20:
        return "<20"
    if v < 40:
        return "20-40"
    if v < 60:
        return "40-60"
    if v < 80:
        return "60-80"
    return "80+"


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan per-user orders.log for trade patterns")
    parser.add_argument(
        "--user",
        default="user-1",
        help="Dashboard user (server/data/users/<user>/logs/orders.log)",
    )
    args = parser.parse_args()
    events = load_events(args.user)
    trades = reconstruct_trades(events)
    if not trades:
        print("No reconstructed trades found.")
        return

    winners = [t for t in trades if t["realized_pnl"] > 0]
    losers = [t for t in trades if t["realized_pnl"] < 0]
    flat = [t for t in trades if t["realized_pnl"] == 0]

    def agg_by_reason(rows: list[dict]) -> list[tuple[str, int, float, float]]:
        by: dict[str, list[float]] = defaultdict(list)
        for t in rows:
            by[t["exit_reason"]].append(t["realized_pnl"])
        out = []
        for reason, pnls in sorted(by.items(), key=lambda x: -len(x[1])):
            out.append((reason, len(pnls), sum(pnls), sum(pnls) / len(pnls)))
        return out

    print(f"=== Trade pattern scan (user={args.user}) ===\n")
    print(f"Total closed (reconstructed): {len(trades)}")
    print(f"Winners: {len(winners)}  Losers: {len(losers)}  Breakeven: {len(flat)}")
    if trades:
        tw = sum(t["realized_pnl"] for t in winners)
        tl = sum(t["realized_pnl"] for t in losers)
        print(f"Sum win P&L: {tw:.2f}  Sum loss P&L: {tl:.2f}  Net: {tw + tl:.2f}\n")

    print("--- Losers by EXIT reason (count, sum P&L, avg P&L) ---")
    for reason, n, s, a in agg_by_reason(losers):
        print(f"  {reason}: n={n} sum={s:.2f} avg={a:.2f}")

    print("\n--- Winners by EXIT reason (count) ---")
    wc = Counter(t["exit_reason"] for t in winners)
    for reason, n in wc.most_common():
        print(f"  {reason}: {n}")

    print("\n--- Losers by symbol (count, sum P&L) ---")
    by_sym: dict[str, list[float]] = defaultdict(list)
    for t in losers:
        by_sym[t["symbol"]].append(t["realized_pnl"])
    for sym in sorted(by_sym.keys(), key=lambda s: sum(by_sym[s])):
        pnls = by_sym[sym]
        print(f"  {sym}: n={len(pnls)} sum={sum(pnls):.2f}")

    print("\n--- Entry side among losers ---")
    print(" ", Counter(t["side"] for t in losers))

    print("\n--- chart_pct bucket: losers vs winners (count) ---")
    lb: Counter[str] = Counter()
    wb: Counter[str] = Counter()
    for t in losers:
        lb[bucket_chart_pct(t["chart_pct"])] += 1
    for t in winners:
        wb[bucket_chart_pct(t["chart_pct"])] += 1
    keys = sorted(set(lb.keys()) | set(wb.keys()), key=lambda k: (k == "unknown", k))
    for k in keys:
        print(f"  {k}: losers={lb.get(k, 0)}  winners={wb.get(k, 0)}")

    print("\n=== Suggested mitigations (from patterns above) ===")
    print(
        "1) TRAILING_STOP_LOSS_HIT heavy: widen trail / delay first trail step / "
        "require stronger EMA separation or higher OB% before entry on choppy symbols."
    )
    print(
        "2) OPPOSITE_CROSSOVER heavy: trend is reversing - tighten entry filter "
        "(e.g. higher min_sep_pct) or reduce size on late-day / low-volatility regimes."
    )
    print(
        "3) EOD_SQUAREOFF losses: ensure NSE square-off is intended; avoid new entries "
        "too close to cutoff; optional hard stop on new entries after a time gate."
    )
    print(
        "4) Symbol-specific underperformance: consider disabling or stricter thresholds "
        "for that symbol until expectancy recovers."
    )


if __name__ == "__main__":
    main()
