"""Per-user trade state, order-log parsing, and WebSocket fan-out."""

from __future__ import annotations

import calendar
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
from typing import List
from zoneinfo import ZoneInfo

from fastapi import WebSocket

from app.constants import DASHBOARD_USERNAME

# Log timestamps and trading day boundaries use IST (matches typical Indian market usage).
IST = ZoneInfo("Asia/Kolkata")

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
PAPER_LINE_PATTERN = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - "
    r"(?P<script>[^|]+) \| ACTION=(?P<action>PAPER_ENTRY|PAPER_EXIT) \| SIDE=(?P<side>BUY|SELL) "
    r"\| PRICE=(?P<price>\d+(?:\.\d+)?) \| REASON=(?P<reason>[^|]+) \| qty=(?P<qty>\d+(?:\.\d+)?)"
)
ORDER_ID_PATTERN = re.compile(r"order_id=(?P<order_id>\d+)")


@dataclass
class UserDataPaths:
    user_root: Path

    @property
    def orders_log(self) -> Path:
        return self.user_root / "logs" / "orders.log"

    @property
    def paper_orders_log(self) -> Path:
        return self.user_root / "logs" / "paper_orders.log"

    @property
    def archive_root(self) -> Path:
        return self.user_root / "archive"


class TradeUserContext:
    def __init__(self, username: str, paths: UserDataPaths):
        self.username = username
        self.paths = paths
        self.live_trades: dict[str, dict] = {}
        self.closed_trades: list[dict] = []
        self.ws_clients: List[WebSocket] = []

    def ensure_dirs(self) -> None:
        self.paths.user_root.mkdir(parents=True, exist_ok=True)
        (self.paths.user_root / "logs").mkdir(parents=True, exist_ok=True)
        self.paths.archive_root.mkdir(parents=True, exist_ok=True)

    @property
    def _manual_controls_path(self) -> Path:
        return self.paths.user_root / "manual_trade_controls.json"

    @property
    def _state_path(self) -> Path:
        return self.paths.user_root / "trading_state.json"

    def _load_manual_controls(self) -> dict:
        path = self._manual_controls_path
        if not path.is_file():
            return {"ignored_trade_ids": [], "entry_price_overrides": {}}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {"ignored_trade_ids": [], "entry_price_overrides": {}}
        ignored = raw.get("ignored_trade_ids") or []
        overrides = raw.get("entry_price_overrides") or {}
        return {
            "ignored_trade_ids": [str(x) for x in ignored if str(x).strip()],
            "entry_price_overrides": {
                str(k): float(v)
                for k, v in dict(overrides).items()
                if str(k).strip()
            },
        }

    def _save_manual_controls(self, controls: dict) -> None:
        payload = {
            "ignored_trade_ids": list(dict.fromkeys(controls.get("ignored_trade_ids") or [])),
            "entry_price_overrides": controls.get("entry_price_overrides") or {},
            "updated_at": datetime.utcnow().isoformat(),
        }
        self._manual_controls_path.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

    def _read_state(self) -> dict:
        if not self._state_path.is_file():
            return {}
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}

    def _write_state(self, state: dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    @staticmethod
    def _calc_unrealized(side: str, entry: float, last_price: float, quantity: float) -> float:
        if side == "BUY":
            return (last_price - entry) * quantity
        if side == "SELL":
            return (entry - last_price) * quantity
        return 0.0

    def _append_manual_log_line(self, message: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - {message}\n"
        self.paths.orders_log.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.orders_log.open("a", encoding="utf-8") as f:
            f.write(line)

    def _sort_closed_trades(self) -> None:
        self.closed_trades.sort(
            key=lambda t: t.get("closed_at") or "",
            reverse=True,
        )

    async def broadcast(self, message: dict) -> None:
        stale: list[WebSocket] = []
        for ws in self.ws_clients:
            try:
                await ws.send_json(message)
            except Exception:
                stale.append(ws)
        for ws in stale:
            if ws in self.ws_clients:
                self.ws_clients.remove(ws)

    @staticmethod
    def _parse_ts(ts_str: str) -> datetime:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")

    @staticmethod
    def _today_ist() -> date:
        return datetime.now(IST).date()

    @classmethod
    def _week_monday_to_friday(cls, week_offset: int = 0) -> list[date]:
        today = cls._today_ist()
        base_monday = today - timedelta(days=today.weekday())
        monday = base_monday - timedelta(days=7 * max(0, week_offset))
        return [monday + timedelta(days=day_offset) for day_offset in range(5)]

    @classmethod
    def _month_calendar_days(cls, month_offset: int = 0) -> tuple[list[date], int, int]:
        """IST calendar month: (all days in month, year, month). month_offset 0 = current IST month."""
        d = cls._today_ist()
        y, m = d.year, d.month
        for _ in range(max(0, int(month_offset))):
            m -= 1
            if m < 1:
                m = 12
                y -= 1
        last = calendar.monthrange(y, m)[1]
        days = [date(y, m, day) for day in range(1, last + 1)]
        return days, y, m

    def _legacy_admin_bucket_order_logs(self) -> list[Path]:
        """Old layout: users/<holder>/archive/<username>/<ts>/logs/orders.log (holder AK07 or legacy admin)."""
        users_dir = self.paths.user_root.parent
        seen: set[str] = set()
        out: list[Path] = []
        for holder in ("AK07", "admin"):
            base = users_dir / holder / "archive" / self.username
            if not base.is_dir():
                continue
            for p in sorted(base.glob("*/logs/orders.log")):
                key = str(p.resolve())
                if key not in seen:
                    seen.add(key)
                    out.append(p)
        return out

    def _get_order_log_files(self) -> list[Path]:
        files: list[Path] = []
        if self.paths.orders_log.exists():
            files.append(self.paths.orders_log)
        if self.paths.archive_root.exists():
            files.extend(sorted(self.paths.archive_root.glob("*/logs/orders.log")))
        files.extend(self._legacy_admin_bucket_order_logs())
        seen = set()
        deduped = []
        for file_path in files:
            resolved = str(file_path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            deduped.append(file_path)
        return deduped

    def _parse_order_events(self) -> list[tuple]:
        order_files = self._get_order_log_files()
        if not order_files:
            return []

        parsed_events = []
        for order_file in order_files:
            lines = order_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            for raw in lines:
                stripped = raw.strip()
                match = LINE_PATTERN.search(stripped)
                if not match:
                    continue
                order_id_match = ORDER_ID_PATTERN.search(stripped)
                order_id = order_id_match.group("order_id") if order_id_match else ""
                parsed_events.append(
                    (
                        self._parse_ts(match.group("ts")),
                        match.group("script").strip(),
                        match.group("action"),
                        match.group("side"),
                        float(match.group("price")),
                        order_id,
                    )
                )

        parsed_events.sort(key=lambda event: event[0])

        deduped = []
        seen = set()
        for event in parsed_events:
            key = (
                event[0].isoformat(timespec="milliseconds"),
                event[1],
                event[2],
                event[3],
                round(float(event[4]), 4),
                event[5],
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(event)

        return deduped

    def _parse_paper_order_events(self) -> list[tuple]:
        path = self.paths.paper_orders_log
        if not path.is_file():
            return []
        parsed_events = []
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = raw.strip()
            match = PAPER_LINE_PATTERN.search(stripped)
            if not match:
                continue
            action = "ENTRY" if match.group("action") == "PAPER_ENTRY" else "EXIT"
            parsed_events.append(
                (
                    self._parse_ts(match.group("ts")),
                    match.group("script").strip(),
                    action,
                    match.group("side"),
                    float(match.group("price")),
                    float(match.group("qty")),
                )
            )

        parsed_events.sort(key=lambda event: event[0])

        deduped = []
        seen = set()
        for event in parsed_events:
            key = (
                event[0].isoformat(timespec="milliseconds"),
                event[1],
                event[2],
                event[3],
                round(float(event[4]), 4),
                round(float(event[5]), 4),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(event)

        return deduped

    def _build_closed_trades_from_paper_log(self, limit: int = 300) -> list[dict]:
        parsed_events = self._parse_paper_order_events()
        if not parsed_events:
            return []
        entries = defaultdict(deque)
        reconstructed: list[dict] = []
        sequence = 0

        for ts, script, action, side, price, qty in parsed_events:
            if action == "ENTRY":
                entries[script].append({"side": side, "price": price, "ts": ts, "qty": qty})
                continue

            if not entries[script]:
                continue

            entry = self._pop_matching_entry(entries[script], side)
            if entry is None:
                continue
            lot = float(entry["qty"])
            if entry["side"] == "BUY" and side == "SELL":
                realized = (price - entry["price"]) * lot
            elif entry["side"] == "SELL" and side == "BUY":
                realized = (entry["price"] - price) * lot
            else:
                realized = 0.0

            sequence += 1
            reconstructed.append(
                {
                    "id": f"paper-{script}-{int(ts.timestamp() * 1000)}-{sequence}",
                    "symbol": script,
                    "side": entry["side"],
                    "quantity": lot,
                    "entry_price": float(entry["price"]),
                    "exit_price": float(price),
                    "last_price": float(price),
                    "unrealized_pnl": 0.0,
                    "realized_pnl": round(realized, 2),
                    "opened_at": entry["ts"].isoformat(),
                    "closed_at": ts.isoformat(),
                }
            )

        reconstructed.reverse()
        return reconstructed[:limit]

    @staticmethod
    def _pop_matching_entry(entry_queue: deque, exit_side: str) -> dict | None:
        expected_entry_side = "BUY" if exit_side == "SELL" else "SELL"
        for idx in range(len(entry_queue) - 1, -1, -1):
            entry = entry_queue[idx]
            if str(entry.get("side")) == expected_entry_side:
                if idx == 0:
                    return entry_queue.popleft()
                if idx == len(entry_queue) - 1:
                    return entry_queue.pop()
                selected = entry
                entry_queue.remove(selected)
                return selected
        return None

    def _daily_realized_pnl_for_days(self, calendar_days: list[date]) -> list[dict]:
        """Realized P&L attributed to EXIT timestamp's calendar date (naive log time = IST wall clock)."""
        day_set = set(calendar_days)
        pnl_by_date = {day: 0.0 for day in calendar_days}

        parsed_events = self._parse_order_events()
        if not parsed_events:
            return [{"date": day.strftime("%Y-%m-%d"), "pnl": 0.0} for day in calendar_days]
        entries = defaultdict(deque)

        for ts, script, action, side, price, _order_id in parsed_events:
            if action == "ENTRY":
                entries[script].append({"side": side, "price": price, "ts": ts})
                continue

            if not entries[script]:
                continue

            entry = self._pop_matching_entry(entries[script], side)
            if entry is None:
                continue
            lot = LOT_SIZES.get(script, 1)

            if entry["side"] == "BUY" and side == "SELL":
                points = price - entry["price"]
            elif entry["side"] == "SELL" and side == "BUY":
                points = entry["price"] - price
            else:
                points = 0.0

            trade_day = ts.date()
            if trade_day in day_set:
                pnl_by_date[trade_day] += points * lot

        return [
            {"date": day.strftime("%Y-%m-%d"), "pnl": round(pnl_by_date[day], 2)}
            for day in calendar_days
        ]

    def _compute_weekly_pnl_from_orders(self, week_offset: int = 0) -> list[dict]:
        weekdays = self._week_monday_to_friday(week_offset)
        return self._daily_realized_pnl_for_days(weekdays)

    def _compute_monthly_pnl_from_orders(self, month_offset: int = 0) -> list[dict]:
        days, _y, _m = self._month_calendar_days(month_offset)
        return self._daily_realized_pnl_for_days(days)

    def _ist_month_summary(self) -> dict:
        """Current IST calendar month realized P&L (from orders.log), for dashboard MTD line."""
        pts = self._compute_monthly_pnl_from_orders(0)
        days, _y, _m = self._month_calendar_days(0)
        return {
            "total": self._weekly_total(pts),
            "range_start": days[0].strftime("%Y-%m-%d"),
            "range_end": days[-1].strftime("%Y-%m-%d"),
        }

    @staticmethod
    def _weekly_total(points: list[dict]) -> float:
        return round(sum(float(point.get("pnl", 0.0) or 0.0) for point in points), 2)

    def _weekly_filter_options(self, count: int = 12) -> list[dict]:
        options = []
        for offset in range(max(1, count)):
            weekdays = self._week_monday_to_friday(offset)
            start_day = weekdays[0].strftime("%Y-%m-%d")
            end_day = weekdays[-1].strftime("%Y-%m-%d")
            label = "Current Week" if offset == 0 else f"{offset} Week Ago"
            options.append(
                {
                    "week_offset": offset,
                    "label": label,
                    "range_start": start_day,
                    "range_end": end_day,
                }
            )
        return options

    def _monthly_filter_options(self, count: int = 12) -> list[dict]:
        options = []
        for offset in range(max(0, count)):
            days, y, m = self._month_calendar_days(offset)
            start_day = days[0].strftime("%Y-%m-%d")
            end_day = days[-1].strftime("%Y-%m-%d")
            if offset == 0:
                label = "This Month"
            elif offset == 1:
                label = "1 Month Ago"
            else:
                label = f"{offset} Months Ago"
            options.append(
                {
                    "month_offset": offset,
                    "label": label,
                    "range_start": start_day,
                    "range_end": end_day,
                    "year": y,
                    "month": m,
                }
            )
        return options

    def _build_closed_trades_from_orders(self, limit: int = 300) -> list[dict]:
        parsed_events = self._parse_order_events()
        if not parsed_events:
            return []
        entries = defaultdict(deque)
        reconstructed: list[dict] = []
        sequence = 0

        for ts, script, action, side, price, _order_id in parsed_events:
            if action == "ENTRY":
                entries[script].append({"side": side, "price": price, "ts": ts})
                continue

            if not entries[script]:
                continue

            entry = self._pop_matching_entry(entries[script], side)
            if entry is None:
                continue
            lot = float(LOT_SIZES.get(script, 1))
            if entry["side"] == "BUY" and side == "SELL":
                realized = (price - entry["price"]) * lot
            elif entry["side"] == "SELL" and side == "BUY":
                realized = (entry["price"] - price) * lot
            else:
                realized = 0.0

            sequence += 1
            reconstructed.append(
                {
                    "id": f"{script}-{int(ts.timestamp() * 1000)}-{sequence}",
                    "symbol": script,
                    "side": entry["side"],
                    "quantity": lot,
                    "entry_price": float(entry["price"]),
                    "exit_price": float(price),
                    "last_price": float(price),
                    "unrealized_pnl": 0.0,
                    "realized_pnl": round(realized, 2),
                    "opened_at": entry["ts"].isoformat(),
                    "closed_at": ts.isoformat(),
                }
            )

        reconstructed.reverse()
        return reconstructed[:limit]

    @staticmethod
    def _trade_date_text(trade: dict) -> str:
        closed_at = str(trade.get("closed_at") or "")
        if len(closed_at) >= 10:
            return closed_at[:10]
        opened_at = str(trade.get("opened_at") or "")
        if len(opened_at) >= 10:
            return opened_at[:10]
        return ""

    @staticmethod
    def _closed_at_calendar_date(trade: dict) -> date | None:
        text = str(trade.get("closed_at") or "").strip()
        if len(text) < 10:
            return None
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    def _compute_symbol_performance(self, days: int = 14) -> dict:
        safe_days = max(1, min(int(days), 366))
        today = self._today_ist()
        cutoff = today - timedelta(days=safe_days - 1)

        effective = self._effective_closed_trades(limit=5000)
        filtered: list[dict] = []
        for trade in effective:
            closed_day = self._closed_at_calendar_date(trade)
            if closed_day is None or closed_day < cutoff:
                continue
            filtered.append(trade)

        by_symbol: dict[str, list[float]] = defaultdict(list)
        for trade in filtered:
            sym = str(trade.get("symbol") or "UNKNOWN").strip() or "UNKNOWN"
            pnl = float(trade.get("realized_pnl") or 0.0)
            by_symbol[sym].append(pnl)

        rows: list[dict] = []
        for symbol in sorted(by_symbol.keys()):
            pnls = by_symbol[symbol]
            n = len(pnls)
            wins = sum(1 for p in pnls if p > 0.0)
            losses = sum(1 for p in pnls if p < 0.0)
            flat = n - wins - losses
            total = round(sum(pnls), 2)
            win_rate = round((wins / n) * 100.0, 1) if n else 0.0
            avg = round(total / n, 2) if n else 0.0
            rows.append(
                {
                    "symbol": symbol,
                    "trades": n,
                    "wins": wins,
                    "losses": losses,
                    "breakeven": flat,
                    "win_rate_pct": win_rate,
                    "total_pnl": total,
                    "avg_pnl": avg,
                }
            )

        rows.sort(key=lambda r: r["total_pnl"], reverse=True)
        return {
            "period_days": safe_days,
            "cutoff_date": cutoff.isoformat(),
            "end_date": today.isoformat(),
            "trade_count": len(filtered),
            "rows": rows,
        }

    @staticmethod
    def _normalize_iso_second(value: str | None) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = text.replace(",", ".")
        if len(text) >= 19:
            return text[:19]
        return text

    def _dedupe_closed_trades(self, trades: list[dict]) -> list[dict]:
        deduped = []
        seen = set()
        for trade in trades:
            key = (
                trade.get("symbol"),
                trade.get("side"),
                round(float(trade.get("entry_price", 0) or 0), 4),
                round(float(trade.get("exit_price", 0) or 0), 4),
                round(float(trade.get("quantity", 0) or 0), 4),
                self._normalize_iso_second(trade.get("opened_at")),
                self._normalize_iso_second(trade.get("closed_at")),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(trade)
        return deduped

    @staticmethod
    def _closed_trade_open_identity_key(trade: dict) -> tuple:
        return (
            trade.get("symbol"),
            trade.get("side"),
            round(float(trade.get("quantity", 0) or 0), 4),
            round(float(trade.get("entry_price", 0) or 0), 4),
            str(trade.get("opened_at") or ""),
        )

    @staticmethod
    def _trade_realized_value(trade: dict) -> float:
        try:
            return float(trade.get("realized_pnl", 0) or 0)
        except Exception:
            return 0.0

    @staticmethod
    def _live_trade_identity_key(trade: dict) -> tuple:
        return (
            trade.get("symbol"),
            trade.get("side"),
            round(float(trade.get("quantity", 0) or 0), 4),
            round(float(trade.get("entry_price", 0) or 0), 4),
            TradeUserContext._normalize_iso_second(trade.get("opened_at")),
        )

    def _dedupe_live_trades(self, trades: list[dict]) -> list[dict]:
        deduped_by_key: dict[tuple, dict] = {}
        for trade in trades:
            key = self._live_trade_identity_key(trade)
            previous = deduped_by_key.get(key)
            current_rank = (
                str(trade.get("opened_at") or ""),
                str(trade.get("id") or ""),
            )
            previous_rank = (
                str(previous.get("opened_at") or ""),
                str(previous.get("id") or ""),
            ) if previous else ("", "")
            if previous is None or current_rank >= previous_rank:
                deduped_by_key[key] = trade
        return list(deduped_by_key.values())

    def upsert_live_trade(self, payload: dict) -> None:
        controls = self._load_manual_controls()
        ignored_ids = set(controls.get("ignored_trade_ids") or [])
        trade_id = str(payload.get("id") or "").strip()
        if trade_id in ignored_ids:
            return
        override_price = (controls.get("entry_price_overrides") or {}).get(trade_id)
        if override_price is not None:
            payload["entry_price"] = float(override_price)
            side = str(payload.get("side") or "").upper()
            qty = float(payload.get("quantity") or 0.0)
            last_price = float(payload.get("last_price") or payload.get("entry_price") or 0.0)
            payload["unrealized_pnl"] = round(
                self._calc_unrealized(side, float(override_price), last_price, qty),
                2,
            )
            payload["entry_price_overridden"] = True
        target_key = self._live_trade_identity_key(payload)
        stale_ids = []
        for trade_id, existing in self.live_trades.items():
            if trade_id == payload["id"]:
                continue
            if self._live_trade_identity_key(existing) == target_key:
                stale_ids.append(trade_id)
        for trade_id in stale_ids:
            self.live_trades.pop(trade_id, None)
        self.live_trades[payload["id"]] = payload

    async def update_manual_entry_price(self, trade_id: str, entry_price: float) -> dict:
        current = self.live_trades.get(trade_id)
        if not current:
            raise ValueError("trade not found")
        if not bool(current.get("manual_execution")):
            raise ValueError("only manual trades can be edited")

        controls = self._load_manual_controls()
        overrides = dict(controls.get("entry_price_overrides") or {})
        overrides[trade_id] = float(entry_price)
        controls["entry_price_overrides"] = overrides
        self._save_manual_controls(controls)

        side = str(current.get("side") or "").upper()
        qty = float(current.get("quantity") or 0.0)
        last_price = float(current.get("last_price") or entry_price)
        current["entry_price"] = float(entry_price)
        current["unrealized_pnl"] = round(
            self._calc_unrealized(side, float(entry_price), last_price, qty),
            2,
        )
        current["entry_price_overridden"] = True
        self.live_trades[trade_id] = current

        state = self._read_state()
        positions = state.get("positions") or {}
        for _symbol, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            if str(pos.get("trade_id") or "") != trade_id:
                continue
            pos["entry_price"] = float(entry_price)
            pos["manual_entry_price"] = float(entry_price)
            pos["manual_entry_updated_at"] = datetime.utcnow().isoformat()
            state["positions"] = positions
            state["timestamp"] = datetime.utcnow().isoformat()
            self._write_state(state)
            break

        self._append_manual_log_line(
            f"{current.get('symbol', '')} | ACTION=MANUAL_ENTRY_EDIT | SIDE={side} | "
            f"PRICE={float(entry_price):.2f} | REASON=USER_OVERRIDE | trade_id={trade_id}"
        )
        await self.broadcast({"type": "trade_updated", "trade": current})
        return current

    async def dismiss_manual_trade(self, trade_id: str) -> dict:
        current = self.live_trades.get(trade_id)
        if not current:
            raise ValueError("trade not found")
        if not bool(current.get("manual_execution")):
            raise ValueError("only manual trades can be removed")

        controls = self._load_manual_controls()
        ignored = list(dict.fromkeys((controls.get("ignored_trade_ids") or []) + [trade_id]))
        controls["ignored_trade_ids"] = ignored
        self._save_manual_controls(controls)

        self.live_trades.pop(trade_id, None)
        state = self._read_state()
        positions = state.get("positions") or {}
        removed = False
        for symbol in list(positions.keys()):
            pos = positions.get(symbol) or {}
            if str(pos.get("trade_id") or "") == trade_id:
                positions.pop(symbol, None)
                removed = True
        if removed:
            state["positions"] = positions
            state["timestamp"] = datetime.utcnow().isoformat()
            self._write_state(state)

        side = str(current.get("side") or "").upper()
        self._append_manual_log_line(
            f"{current.get('symbol', '')} | ACTION=MANUAL_TRACK_DISMISSED | SIDE={side} | "
            f"PRICE={float(current.get('last_price') or current.get('entry_price') or 0.0):.2f} | "
            f"REASON=USER_NOT_TRADED | trade_id={trade_id}"
        )
        await self.broadcast({"type": "trade_closed", "trade": current})
        return {"removed": True, "trade_id": trade_id}

    def _effective_closed_trades(self, limit: int = 1000) -> list[dict]:
        reconstructed = self._build_closed_trades_from_orders(limit=limit)
        by_open_identity: dict[tuple, dict] = {}

        for trade in reconstructed:
            by_open_identity[self._closed_trade_open_identity_key(trade)] = trade

        for trade in self.closed_trades:
            key = self._closed_trade_open_identity_key(trade)
            existing = by_open_identity.get(key)
            if existing is None:
                by_open_identity[key] = trade

        merged = self._dedupe_closed_trades(list(by_open_identity.values()))
        merged.sort(key=lambda t: t.get("closed_at") or "", reverse=True)
        return merged[:limit]

    def _closed_trade_dates(self, trades: list[dict]) -> list[str]:
        dates = {d for d in (self._trade_date_text(trade) for trade in trades) if d}
        return sorted(dates, reverse=True)

    def _filter_closed_trades_by_date(
        self, trades: list[dict], date_text: str | None
    ) -> list[dict]:
        if not date_text:
            return trades
        return [trade for trade in trades if self._trade_date_text(trade) == date_text]

    def _trading_scripts_payload(self) -> dict:
        from app.config.paths import ensure_repo_and_lib_on_path

        ensure_repo_and_lib_on_path()
        from trading_preferences_store import read_trading_preferences
        from trading_script_constants import AVAILABLE_SCRIPT_NAMES

        prefs = read_trading_preferences(self.username)
        ens = prefs.get("enabled_scripts")
        return {
            "available_scripts": list(AVAILABLE_SCRIPT_NAMES),
            "enabled_scripts": ens,
            "trading_scope_mode": "all" if ens is None else "subset",
        }

    def dashboard_initial_dict(self) -> dict:
        today_text = self._today_ist().isoformat()
        effective_closed = self._effective_closed_trades(limit=2000)
        available_dates = self._closed_trade_dates(effective_closed)
        selected_date = today_text if today_text in available_dates else (available_dates[0] if available_dates else today_text)
        date_filtered_closed = self._filter_closed_trades_by_date(effective_closed, selected_date)
        weekly_points = self._compute_weekly_pnl_from_orders(week_offset=0)
        monthly_points = self._compute_monthly_pnl_from_orders(month_offset=0)
        return {
            "live_trades": self._dedupe_live_trades(list(self.live_trades.values())),
            "closed_trades": date_filtered_closed,
            "closed_trade_dates": available_dates,
            "closed_trade_selected_date": selected_date,
            "weekly_pnl": weekly_points,
            "weekly_total": self._weekly_total(weekly_points),
            "weekly_selected_offset": 0,
            "weekly_filter_options": self._weekly_filter_options(count=12),
            "monthly_pnl": monthly_points,
            "monthly_total": self._weekly_total(monthly_points),
            "monthly_selected_offset": 0,
            "monthly_filter_options": self._monthly_filter_options(count=12),
            "ist_month": self._ist_month_summary(),
            "server_time": datetime.utcnow().isoformat(),
            "data_user": self.username,
            **self._trading_scripts_payload(),
        }

    def paper_live_trades_from_state(self) -> list[dict]:
        """Open paper positions from bot `trading_state.json` (same shape as live trades for the UI)."""
        path = self.paths.user_root / "trading_state.json"
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return []
        paper = raw.get("paper_positions") or {}
        if not isinstance(paper, dict):
            return []
        out: list[dict] = []
        for symbol, pos in paper.items():
            if not isinstance(pos, dict):
                continue
            sym = str(symbol).strip()
            if not sym:
                continue
            side = str(pos.get("type") or "BUY").upper()
            entry = float(pos.get("entry_price") or 0.0)
            qty = float(pos.get("quantity") or 0.0)
            if qty <= 0:
                qty = 1.0
            last_raw = pos.get("last_polled_price")
            try:
                last = float(last_raw) if last_raw is not None else entry
            except (TypeError, ValueError):
                last = entry
            if side == "BUY":
                unrealized = (last - entry) * qty
            elif side == "SELL":
                unrealized = (entry - last) * qty
            else:
                unrealized = 0.0
            trade_id = str(pos.get("trade_id") or f"{sym}-{pos.get('entry_time', '')}")
            cp = pos.get("chart_percent")
            wp = pos.get("win_percent")
            out.append(
                {
                    "id": trade_id,
                    "symbol": sym,
                    "side": side,
                    "quantity": qty,
                    "entry_price": entry,
                    "stop_loss": float(pos.get("stop_loss") or entry),
                    "target_price": float(pos.get("target_price") or entry),
                    "chart_percent": float(cp) if cp is not None else None,
                    "chart_volume": pos.get("chart_volume"),
                    "win_percent": float(wp) if wp is not None else None,
                    "last_price": last,
                    "unrealized_pnl": round(unrealized, 2),
                    "opened_at": str(pos.get("entry_time") or ""),
                    "closed_at": None,
                }
            )
        out.sort(key=lambda t: str(t.get("opened_at") or ""), reverse=True)
        return out

    def paper_closed_trades_response(self, date: str | None) -> dict:
        if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            return {
                "closed_trades": [],
                "closed_trade_dates": [],
                "selected_date": date,
                "paper_total_realized": 0.0,
            }
        all_trades = self._build_closed_trades_from_paper_log(limit=2000)
        available_dates = self._closed_trade_dates(all_trades)
        total_realized = round(
            sum(self._trade_realized_value(t) for t in all_trades),
            2,
        )
        today_text = self._today_ist().isoformat()
        effective_date = date
        if not effective_date:
            effective_date = (
                today_text
                if today_text in available_dates
                else (available_dates[0] if available_dates else today_text)
            )
        return {
            "closed_trades": self._filter_closed_trades_by_date(all_trades, effective_date),
            "closed_trade_dates": available_dates,
            "selected_date": effective_date,
            "paper_total_realized": total_realized,
        }

    def closed_trades_response(self, date: str | None) -> dict:
        if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            return {"closed_trades": [], "closed_trade_dates": [], "selected_date": date}
        effective_closed = self._effective_closed_trades(limit=2000)
        return {
            "closed_trades": self._filter_closed_trades_by_date(effective_closed, date),
            "closed_trade_dates": self._closed_trade_dates(effective_closed),
            "selected_date": date,
        }

    def weekly_pnl_dict(self, week_offset: int) -> dict:
        safe_offset = max(0, int(week_offset))
        weekly_points = self._compute_weekly_pnl_from_orders(week_offset=safe_offset)
        return {
            "weekly_pnl": weekly_points,
            "weekly_total": self._weekly_total(weekly_points),
            "weekly_selected_offset": safe_offset,
            "weekly_filter_options": self._weekly_filter_options(count=12),
            "ist_month": self._ist_month_summary(),
        }

    def monthly_pnl_dict(self, month_offset: int) -> dict:
        safe_offset = max(0, int(month_offset))
        monthly_points = self._compute_monthly_pnl_from_orders(month_offset=safe_offset)
        return {
            "monthly_pnl": monthly_points,
            "monthly_total": self._weekly_total(monthly_points),
            "monthly_selected_offset": safe_offset,
            "monthly_filter_options": self._monthly_filter_options(count=12),
            "ist_month": self._ist_month_summary(),
        }

    async def apply_trade_close(self, payload: dict) -> dict:
        if not payload.get("closed_at"):
            payload["closed_at"] = datetime.utcnow().isoformat()

        if payload.get("realized_pnl") is None:
            if payload.get("unrealized_pnl") is not None:
                payload["realized_pnl"] = round(float(payload.get("unrealized_pnl") or 0), 2)
            elif payload.get("entry_price") is not None and payload.get("exit_price") is not None:
                qty = float(payload.get("quantity") or 0)
                entry = float(payload.get("entry_price") or 0)
                exit_price = float(payload.get("exit_price") or 0)
                side = payload.get("side")
                if side == "BUY":
                    payload["realized_pnl"] = round((exit_price - entry) * qty, 2)
                else:
                    payload["realized_pnl"] = round((entry - exit_price) * qty, 2)
            else:
                payload["realized_pnl"] = 0.0
        payload["unrealized_pnl"] = 0.0

        self.live_trades.pop(payload["id"], None)
        close_key = self._live_trade_identity_key(payload)
        for trade_id in list(self.live_trades.keys()):
            existing = self.live_trades.get(trade_id)
            if existing and self._live_trade_identity_key(existing) == close_key:
                self.live_trades.pop(trade_id, None)
        self.closed_trades.insert(0, payload)
        self._sort_closed_trades()
        await self.broadcast({"type": "trade_closed", "trade": payload})
        await self.broadcast(
            {
                "type": "pnl_update",
                "weekly_pnl": self._compute_weekly_pnl_from_orders(week_offset=0),
                "ist_month": self._ist_month_summary(),
            }
        )
        return payload


_contexts: dict[str, TradeUserContext] = {}


def _safe_username(username: str) -> str:
    u = (username or "").strip()
    if not u or any(c in u for c in "/\\:\0"):
        return DASHBOARD_USERNAME
    return u


def get_trade_context(repo: Path, username: str) -> TradeUserContext:
    safe = _safe_username(username)
    if safe not in _contexts:
        ur = repo / "src" / "server" / "data" / "users" / safe
        paths = UserDataPaths(user_root=ur)
        ctx = TradeUserContext(safe, paths)
        ctx.ensure_dirs()
        _contexts[safe] = ctx
    return _contexts[safe]
