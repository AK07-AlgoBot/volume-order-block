from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "SENSEX": 20,
    "CRUDE": 100,
    "GOLDMINI": 1,
    "SILVERMINI": 5,
}

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - "
    r"(?P<script>[^|]+)\| ACTION=(?P<action>[^|]+)\| SIDE=(?P<side>[^|]+)\| "
    r"PRICE=(?P<price>-?\d+(?:\.\d+)?)\s*\| REASON=(?P<reason>[^|]+)"
    r"(?:\s*\|\s*(?P<extra>.*))?$"
)


@dataclass
class Event:
    ts: datetime
    script: str
    action: str
    side: str
    price: float
    reason: str
    extra: dict[str, str]


def parse_float(text: str | None) -> float | None:
    if text is None:
        return None
    value = str(text).strip().rstrip("%")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_extra(extra_text: str | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not extra_text:
        return parsed
    for token in extra_text.split(";"):
        item = token.strip()
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def list_order_logs(include_archive: bool, user: str) -> list[Path]:
    from upstox_credentials_store import (
        legacy_admin_bucket_order_logs,
        sanitize_username,
        user_archive_order_logs,
        user_data_dir,
    )

    ud = user_data_dir(sanitize_username(user))
    order_log = ud / "logs" / "orders.log"
    files: list[Path] = []
    if order_log.exists():
        files.append(order_log)
    if include_archive:
        files.extend(user_archive_order_logs(user))
        files.extend(legacy_admin_bucket_order_logs(user))
    deduped: list[Path] = []
    seen: set[str] = set()
    for f in files:
        key = str(f.resolve())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)
    return deduped


def read_events(
    include_archive: bool, date_filter: str | None, user: str
) -> list[Event]:
    events: list[Event] = []
    for order_file in list_order_logs(include_archive, user):
        text = order_file.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            match = LINE_RE.search(line.strip())
            if not match:
                continue
            ts = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S,%f")
            if date_filter and ts.strftime("%Y-%m-%d") != date_filter:
                continue
            events.append(
                Event(
                    ts=ts,
                    script=match.group("script").strip(),
                    action=match.group("action").strip().upper(),
                    side=match.group("side").strip().upper(),
                    price=float(match.group("price")),
                    reason=match.group("reason").strip(),
                    extra=parse_extra(match.group("extra")),
                )
            )
    events.sort(key=lambda e: e.ts)
    return events


def estimate_bucket(trade_prob: float | None) -> str:
    if trade_prob is None:
        return "UNKNOWN"
    if trade_prob >= 70:
        return "HIGH"
    if trade_prob >= 50:
        return "MEDIUM"
    return "LOW"


def compute_reports(events: list[Event]) -> tuple[list[dict], list[dict]]:
    open_entries: dict[str, deque[Event]] = defaultdict(deque)
    completed: list[dict] = []
    skip_rows: list[dict] = []

    for ev in events:
        if ev.action == "SKIP":
            prob = parse_float(ev.extra.get("trade_prob"))
            skip_rows.append(
                {
                    "script": ev.script,
                    "reason": ev.reason,
                    "prob": prob,
                    "bucket": ev.extra.get("trade_prob_bucket", estimate_bucket(prob)),
                }
            )
            continue

        if ev.action == "ENTRY":
            open_entries[ev.script].append(ev)
            continue

        if ev.action != "EXIT":
            continue

        if not open_entries[ev.script]:
            continue

        entry = open_entries[ev.script].popleft()
        lot = LOT_SIZES.get(ev.script, 1)
        if entry.side == "BUY" and ev.side == "SELL":
            points = ev.price - entry.price
        elif entry.side == "SELL" and ev.side == "BUY":
            points = entry.price - ev.price
        else:
            continue

        pnl = points * lot
        prob = parse_float(entry.extra.get("trade_prob"))
        bucket = entry.extra.get("trade_prob_bucket", estimate_bucket(prob))
        sl = parse_float(entry.extra.get("sl"))
        risk_points = abs(entry.price - sl) if sl is not None else None
        r_mult = (points / risk_points) if (risk_points and risk_points > 0) else None

        completed.append(
            {
                "script": ev.script,
                "pnl": pnl,
                "points": points,
                "prob": prob,
                "bucket": bucket,
                "r_mult": r_mult,
            }
        )

    return completed, skip_rows


def summarize_completed(completed: list[dict]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in completed:
        grouped[row["bucket"]].append(row)

    for bucket, rows in grouped.items():
        n = len(rows)
        wins = sum(1 for r in rows if r["pnl"] > 0)
        total = sum(r["pnl"] for r in rows)
        avg = total / n if n else 0.0
        rr_vals = [r["r_mult"] for r in rows if r["r_mult"] is not None]
        avg_r = (sum(rr_vals) / len(rr_vals)) if rr_vals else None
        summary[bucket] = {
            "count": n,
            "win_rate": (wins / n * 100.0) if n else 0.0,
            "total_pnl": total,
            "avg_pnl": avg,
            "avg_r": avg_r,
        }
    return summary


def summarize_skips(skip_rows: list[dict]) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in skip_rows:
        grouped[row["reason"]].append(row)
    out: dict[str, dict] = {}
    for reason, rows in grouped.items():
        probs = [r["prob"] for r in rows if r["prob"] is not None]
        out[reason] = {
            "count": len(rows),
            "avg_prob": (sum(probs) / len(probs)) if probs else None,
        }
    return out


def suggest_cutoff(completed: list[dict]) -> tuple[int | None, dict | None]:
    eligible = [r for r in completed if r["prob"] is not None]
    if len(eligible) < 5:
        return None, None

    best_threshold = None
    best_stats = None
    for threshold in range(40, 86, 5):
        rows = [r for r in eligible if float(r["prob"]) >= threshold]
        if len(rows) < 3:
            continue
        wins = sum(1 for r in rows if r["pnl"] > 0)
        total = sum(r["pnl"] for r in rows)
        avg = total / len(rows)
        stats = {
            "count": len(rows),
            "win_rate": wins / len(rows) * 100.0,
            "avg_pnl": avg,
            "total_pnl": total,
        }
        if best_stats is None or stats["avg_pnl"] > best_stats["avg_pnl"]:
            best_threshold = threshold
            best_stats = stats
    return best_threshold, best_stats


def print_report(completed: list[dict], skips: list[dict]) -> None:
    print("=" * 84)
    print("TRADE PROBABILITY REPORT")
    print("=" * 84)
    print(f"Completed trades analyzed: {len(completed)}")
    print(f"Skipped signals analyzed:  {len(skips)}")
    print("-" * 84)

    bucket_summary = summarize_completed(completed)
    for bucket in ("HIGH", "MEDIUM", "LOW", "UNKNOWN"):
        s = bucket_summary.get(bucket)
        if not s:
            continue
        avg_r_text = f"{s['avg_r']:.2f}" if s["avg_r"] is not None else "NA"
        print(
            f"{bucket:7} | count={s['count']:3d} | win_rate={s['win_rate']:6.2f}% "
            f"| avg_pnl={s['avg_pnl']:10.2f} | total_pnl={s['total_pnl']:10.2f} | avg_R={avg_r_text}"
        )

    print("-" * 84)
    skip_summary = summarize_skips(skips)
    if skip_summary:
        print("Skip reasons:")
        for reason, s in sorted(skip_summary.items(), key=lambda kv: kv[1]["count"], reverse=True):
            avg_prob_text = f"{s['avg_prob']:.1f}" if s["avg_prob"] is not None else "NA"
            print(f"- {reason}: count={s['count']}, avg_trade_prob={avg_prob_text}")
    else:
        print("Skip reasons: none")

    print("-" * 84)
    cutoff, stats = suggest_cutoff(completed)
    if cutoff is None or stats is None:
        print("Cutoff suggestion: Not enough completed trades with trade_prob yet.")
    else:
        print(
            f"Suggested cutoff: trade_prob >= {cutoff} "
            f"(count={stats['count']}, win_rate={stats['win_rate']:.2f}%, "
            f"avg_pnl={stats['avg_pnl']:.2f}, total_pnl={stats['total_pnl']:.2f})"
        )
    print("=" * 84)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze trade_prob performance from orders.log")
    parser.add_argument("--date", help="Filter by YYYY-MM-DD", default=None)
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Use only active orders.log (exclude archive/*/logs/orders.log)",
    )
    parser.add_argument(
        "--user",
        default="user-1",
        help="Dashboard user (server/data/users/<user>/logs/orders.log)",
    )
    args = parser.parse_args()

    if args.date:
        datetime.strptime(args.date, "%Y-%m-%d")

    events = read_events(
        include_archive=not args.no_archive,
        date_filter=args.date,
        user=args.user,
    )
    completed, skips = compute_reports(events)
    print_report(completed, skips)


if __name__ == "__main__":
    main()

