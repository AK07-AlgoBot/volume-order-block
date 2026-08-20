"""Copy Kite — poll a leader Kite account and fan-out fills via AK07 OMS.

Leader (default Arun) is read-only. Followers get the same FNO contract through
Upstox / Kite / Groww using each user's Copy Kite lot allotment.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, time as dtime
from typing import Any, Final
from zoneinfo import ZoneInfo

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.constants import STRATEGY_COPY_KITE
from app.services import cache_manager, performance_store, telegram_notifier
from app.services.breakout_order_fanout import (
    legs_summary,
    leg_usernames,
    place_copy_orders,
)
from app.services.engine_intraday import entries_globally_blocked
from app.services.kite_engine import KiteClient, lookup_kite_fno_instrument
from app.services.upstox_engine import MOCK_MODE, build_upstox_client

logger = logging.getLogger("ak07.kite_copy")

IST: Final = ZoneInfo("Asia/Kolkata")
POLL_SECONDS: Final[float] = float(os.environ.get("AK07_COPY_POLL_SECONDS", "3"))
LEADER_USER: Final[str] = (os.environ.get("AK07_COPY_LEADER_USER") or "Arun").strip()
BOT_END: Final[dtime] = dtime(15, 30)
SESSION_START: Final[dtime] = dtime(9, 15)
_SKIP_TAGS: Final[tuple[str, ...]] = ("ak07s3", "ak07_engine", "ak07copy", "ak07")
_FNO_EXCHANGES: Final[frozenset[str]] = frozenset({"NFO", "BFO"})
_COMPLETE: Final[frozenset[str]] = frozenset({"COMPLETE", "COMPLETED"})


def _now() -> datetime:
    return datetime.now(IST)


def _copyable(order: dict[str, Any]) -> bool:
    status = str(order.get("status") or "").upper()
    if status not in _COMPLETE:
        return False
    exchange = str(order.get("exchange") or "").upper()
    if exchange not in _FNO_EXCHANGES:
        return False
    try:
        filled = int(order.get("filled_quantity") or order.get("quantity") or 0)
    except (TypeError, ValueError):
        filled = 0
    if filled <= 0:
        return False
    tag = str(order.get("tag") or "").lower()
    if any(skip in tag for skip in _SKIP_TAGS):
        return False
    side = str(order.get("transaction_type") or "").upper()
    return side in ("BUY", "SELL")


def _apply_leader_fill(
    positions: dict[str, dict[str, Any]],
    symbol: str,
    side: str,
    qty: int,
    avg: float,
    now: datetime,
    index_code: str,
) -> dict[str, Any] | None:
    """Update leader net position. Return a closed-leg dict when qty is reduced."""
    if qty <= 0 or not symbol:
        return None
    delta = qty if side == "BUY" else -qty
    pos = positions.get(symbol)
    if not pos or int(pos.get("qty") or 0) == 0:
        positions[symbol] = {
            "qty": delta,
            "avg": avg,
            "opened_at": now.isoformat(),
            "index_code": index_code,
        }
        return None
    old_qty = int(pos.get("qty") or 0)
    old_avg = float(pos.get("avg") or 0)
    same_way = (old_qty > 0 and delta > 0) or (old_qty < 0 and delta < 0)
    if same_way:
        new_qty = old_qty + delta
        positions[symbol] = {
            "qty": new_qty,
            "avg": ((abs(old_qty) * old_avg) + (qty * avg)) / abs(new_qty) if new_qty else avg,
            "opened_at": str(pos.get("opened_at") or now.isoformat()),
            "index_code": str(pos.get("index_code") or index_code),
        }
        return None
    closed_qty = min(abs(old_qty), qty)
    if old_qty > 0:
        direction = "LONG"
        pnl = avg - old_avg
    else:
        direction = "SHORT"
        pnl = old_avg - avg
    remaining = old_qty + delta
    closed = {
        "direction": direction,
        "entry": old_avg,
        "exit": avg,
        "pnl_points": pnl,
        "entry_at": str(pos.get("opened_at") or ""),
        "index_code": str(pos.get("index_code") or index_code),
        "closed_qty": closed_qty,
    }
    if remaining == 0:
        positions.pop(symbol, None)
    else:
        positions[symbol] = {
            "qty": remaining,
            "avg": avg if (old_qty > 0) != (remaining > 0) else old_avg,
            "opened_at": now.isoformat() if (old_qty > 0) != (remaining > 0) else str(pos.get("opened_at") or now.isoformat()),
            "index_code": index_code if (old_qty > 0) != (remaining > 0) else str(pos.get("index_code") or index_code),
        }
    return closed


class KiteCopyEngine:
    def __init__(self) -> None:
        self.paper = bool(MOCK_MODE)
        self.leader = KiteClient(LEADER_USER) if not MOCK_MODE else None
        self.upstox = None if MOCK_MODE else build_upstox_client()
        self.seen: set[str] = set()
        self.seeded = False
        self.signal_log: list[str] = []
        self.setup_label = f"Waiting for {LEADER_USER} Kite fills"
        self.copies_today = 0
        self.trade_day = ""
        self.copied_fills: list[dict[str, Any]] = []
        self._positions: dict[str, dict[str, Any]] = {}
        self._hydrate()
        logger.info(
            "Copy Kite live | leader=%s poll=%.0fs paper=%s fan-out all live except leader",
            LEADER_USER,
            POLL_SECONDS,
            self.paper,
        )

    def run(self) -> None:
        while True:
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception as exc:
                logger.exception("Copy Kite tick error: %s", exc)
            time.sleep(max(0.5, POLL_SECONDS - (time.monotonic() - t0)))

    def tick(self) -> None:
        now = _now()
        self._roll_day(now)
        if self.leader:
            self.leader.refresh_auth_from_disk()
        if now.time() < SESSION_START or now.time() >= BOT_END:
            self.setup_label = "Session closed"
            self._publish(now)
            return
        if self.paper:
            self.setup_label = "PAPER — copy engine idle"
            self._publish(now)
            return
        if not self.leader or not self.leader.has_token():
            self.setup_label = f"No Kite token for {LEADER_USER}"
            self._publish(now)
            return
        if entries_globally_blocked():
            self.setup_label = "Kill switch — not copying"
            self._publish(now)
            return
        orders = self.leader.get_orders()
        if not self.seeded:
            self.seen = {
                str(row.get("order_id") or "")
                for row in orders
                if str(row.get("order_id") or "")
            }
            self.seeded = True
            self.setup_label = f"Seeded {len(self.seen)} existing {LEADER_USER} orders"
            logger.info("Copy Kite seeded %d order ids — will copy new fills only", len(self.seen))
            self._publish(now)
            return

        for row in orders:
            oid = str(row.get("order_id") or "")
            if not oid or oid in self.seen:
                continue
            if not _copyable(row):
                if str(row.get("status") or "").upper() in _COMPLETE:
                    self.seen.add(oid)
                continue
            self.seen.add(oid)
            self._copy_fill(row, now)

        self.setup_label = f"Watching {LEADER_USER} Kite · copies {self.copies_today}"
        self._publish(now)

    def _copy_fill(self, order: dict[str, Any], now: datetime) -> None:
        exchange = str(order.get("exchange") or "NFO").upper()
        symbol = str(order.get("tradingsymbol") or "").strip()
        side = str(order.get("transaction_type") or "").upper()
        inst = lookup_kite_fno_instrument(exchange, symbol)
        if not inst:
            logger.error("Copy Kite skip %s:%s — unknown instrument", exchange, symbol)
            return
        kind = "options" if inst["instrument_type"] in ("CE", "PE") else "futures"
        try:
            filled = int(order.get("filled_quantity") or order.get("quantity") or 0)
        except (TypeError, ValueError):
            filled = 0
        try:
            avg = float(order.get("average_price") or 0)
        except (TypeError, ValueError):
            avg = 0.0
        index_code = str(inst["index_code"])
        legs = place_copy_orders(
            index_code=index_code,
            side=side,
            lot_size=int(inst["lot_size"]),
            kite_exchange=exchange,
            kite_symbol=symbol,
            instrument_kind=kind,
            option_strike=int(inst.get("strike") or 0),
            option_type=str(inst.get("option_type") or ""),
            expiry=str(inst.get("expiry") or ""),
            upstox_market_client=self.upstox,
            global_paper=self.paper,
            exclude_usernames=frozenset({LEADER_USER}),
        )
        self.copies_today += 1
        note = legs_summary(legs) if legs else "no legs"
        line = (
            f"{side} {index_code} {symbol} "
            f"{inst.get('strike') or ''}{inst.get('option_type') or ''} [{note}]"
        )
        self.signal_log.append(line)
        self.signal_log = self.signal_log[-20:]
        self.copied_fills.append(
            {
                "at": now.isoformat(),
                "side": side,
                "index": index_code,
                "contract": symbol,
                "leader_price": round(avg, 2),
                "followers": note,
            }
        )
        self.copied_fills = self.copied_fills[-50:]
        logger.info("Copy Kite %s", line)
        closed = _apply_leader_fill(self._positions, symbol, side, filled, avg, now, index_code)
        if closed and legs:
            self._record_closed(closed, symbol, inst, note, legs)
        telegram_notifier.notify_trade_execution(
            index_name=f"Copy Kite {index_code} ({symbol})",
            trade_type="LONG" if side == "BUY" else "SHORT",
            entry_price=avg,
            target_price=0.0,
            sl_price=0.0,
            component_sentiment=str(inst.get("option_type") or kind),
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _record_closed(
        self,
        closed: dict[str, Any],
        symbol: str,
        inst: dict[str, Any],
        note: str,
        legs: list[dict[str, Any]] | None = None,
    ) -> None:
        performance_store.record_completed_trade(
            strategy=performance_store.STRATEGY_COPY_KITE,
            strategy_id=STRATEGY_COPY_KITE,
            symbol=str(closed.get("index_code") or inst.get("index_code") or ""),
            direction=str(closed.get("direction") or ""),
            entry_price=float(closed.get("entry") or 0),
            exit_price=float(closed.get("exit") or 0),
            pnl_points=float(closed.get("pnl_points") or 0),
            exit_reason="COPIED_CLOSE",
            entry_at=str(closed.get("entry_at") or ""),
            paper_trading=self.paper,
            extra={
                "contract_label": symbol,
                "option_strike": inst.get("strike") or None,
                "option_type": inst.get("option_type") or "",
                "copies": note,
                "participants": leg_usernames(legs),
            },
        )

    def _roll_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self.trade_day == today:
            return
        self.trade_day = today
        self.seen = set()
        self.seeded = False
        self.copies_today = 0
        self.signal_log = []
        self.copied_fills = []
        self.setup_label = "New session"

    def _hydrate(self) -> None:
        raw = cache_manager.get_json(cache_manager.COPY_KITE_STATE_KEY)
        if not isinstance(raw, dict):
            return
        positions = raw.get("leader_positions") or {}
        if isinstance(positions, dict):
            self._positions = {
                str(key): value
                for key, value in positions.items()
                if str(key).strip() and isinstance(value, dict)
            }
        today = datetime.now(IST).date().isoformat()
        if str(raw.get("trade_day") or "") != today:
            return
        self.trade_day = today
        self.copies_today = int(raw.get("copies_today") or 0)
        self.setup_label = str(raw.get("setup_label") or self.setup_label)
        seen = raw.get("seen_order_ids") or []
        if isinstance(seen, list):
            self.seen = {str(x) for x in seen if x}
            self.seeded = True
        fills = raw.get("copied_fills") or []
        if isinstance(fills, list):
            self.copied_fills = [row for row in fills if isinstance(row, dict)]

    def _publish(self, now: datetime) -> None:
        cache_manager.set_json(
            cache_manager.COPY_KITE_STATE_KEY,
            {
                "timestamp": now.isoformat(),
                "strategy": "Copy Kite — Arun mirror",
                "strategy_id": STRATEGY_COPY_KITE,
                "paper_trading": self.paper,
                "leader": LEADER_USER,
                "trade_day": self.trade_day,
                "copies_today": self.copies_today,
                "setup_label": self.setup_label,
                "seen_order_ids": list(self.seen)[-400:],
                "signals": self.signal_log[-8:],
                "copied_fills": self.copied_fills[-50:],
                "leader_positions": self._positions,
            },
            ttl_seconds=86_400,
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    KiteCopyEngine().run()


if __name__ == "__main__":
    main()
