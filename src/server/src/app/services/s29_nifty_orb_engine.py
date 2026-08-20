"""Strategy 29 — Nifty 9:18 ORB live (nifty3v4 rules).

Market data: AK07 Upstox Nifty futures (1-minute OR, then LTP).
Options: S3 ITM CE/PE pick, fan-out to live users' brokers.
First winning trade locks new entries for the rest of the day.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Final
from urllib.parse import quote
from zoneinfo import ZoneInfo

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.constants import S29_ORB_INDICES
from app.services import cache_manager, performance_store, telegram_notifier
from app.services.breakout_order_fanout import (
    catchup_s29_legs,
    legs_summary,
    place_s29_entries,
    place_s29_exits,
    position_legs,
)
from app.services.engine_intraday import entries_globally_blocked, profit_target_engaged
from app.services.upstox_engine import (
    INDEX_CONFIGS,
    MOCK_MODE,
    UpstoxClient,
    build_upstox_client,
)

logger = logging.getLogger("ak07.s29_nifty_orb")

IST: Final = ZoneInfo("Asia/Kolkata")
INDEX_CODE: Final[str] = S29_ORB_INDICES[0]

ENTRY_BUFFER: Final[float] = float(os.environ.get("S29_ENTRY_BUFFER", "3"))
ORB_START: Final[dtime] = dtime(9, 18)
ORB_END: Final[dtime] = dtime(9, 21)
NO_NEW_ENTRY_AFTER: Final[dtime] = dtime(15, 15)
FORCE_EXIT_TIME: Final[dtime] = dtime(15, 25)
BOT_END_TIME: Final[dtime] = dtime(15, 30)
MAX_TRADES_PER_DAY: Final[int] = int(os.environ.get("S29_MAX_TRADES_PER_DAY", "2"))
DAILY_MAX_LOSS: Final[float] = float(os.environ.get("S29_DAILY_MAX_LOSS", "-4000"))
MAX_LOSS_PER_TRADE: Final[float] = float(os.environ.get("S29_MAX_LOSS_PER_TRADE", "1900"))
POLL_SECONDS: Final[float] = float(os.environ.get("S29_POLL_SECONDS", "3"))
RANGE_TARGET_MULT: Final[float] = 2.0


@dataclass
class S29Position:
    direction: str  # LONG = CE, SHORT = PE
    option_side: str
    entry_fut: float
    sl_fut: float
    tgt_fut: float
    option_sl: float | None
    premium_entry: float | None
    instrument_key: str
    option_strike: int
    option_type: str
    lots: int
    lot_size: int
    opened_at: str
    order_legs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class S29State:
    trade_day: str = ""
    fut_key: str = ""
    fut_label: str = ""
    fut_ltp: float | None = None
    index_spot: float | None = None
    orb_high: float | None = None
    orb_low: float | None = None
    orb_range: float | None = None
    orb_done: bool = False
    ce_taken: bool = False
    pe_taken: bool = False
    trades_today: int = 0
    daily_pnl_inr: float = 0.0
    win_lock: bool = False
    position: S29Position | None = None
    setup_label: str = "Pre-session"
    signal_log: list[str] = field(default_factory=list)
    last_fanout_catchup_mono: float = 0.0


def _now() -> datetime:
    return datetime.now(IST)


def _parse_candle_row(row: Any) -> dict[str, float] | None:
    if isinstance(row, dict):
        ts_raw = row.get("timestamp") or row.get("time") or row.get("start_time")
        try:
            return {
                "timestamp": datetime.fromisoformat(str(ts_raw)).isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(row, (list, tuple)) and len(row) >= 5:
        try:
            return {
                "timestamp": datetime.fromisoformat(str(row[0])).isoformat(),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
            }
        except (ValueError, TypeError, IndexError):
            return None
    return None


def _candle_time(ts: str) -> dtime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).time().replace(second=0, microsecond=0)


class S29NiftyOrbEngine:
    def __init__(self) -> None:
        self.paper = bool(MOCK_MODE)
        self.cfg = INDEX_CONFIGS[INDEX_CODE]
        self.client: UpstoxClient | None = None if MOCK_MODE else build_upstox_client()
        self.state = S29State()
        self._mock_fut = 24_200.0
        self._hydrate()
        logger.info(
            "S29 nifty3v4 live | paper=%s mock=%s | OR 09:18-09:21 1m FUT | "
            "LTP +%.0f pts | ITM fan-out | poll=%.0fs",
            self.paper, MOCK_MODE, ENTRY_BUFFER, POLL_SECONDS,
        )

    def run(self) -> None:
        while True:
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception as exc:
                logger.exception("S29 tick error: %s", exc)
            time.sleep(max(0.5, POLL_SECONDS - (time.monotonic() - t0)))

    def tick(self) -> None:
        now = _now()
        if self.client:
            self.client.refresh_access_token_from_disk()
        self._roll_day(now)

        if now.time() >= BOT_END_TIME and self.state.position is None:
            self.state.setup_label = "Session over"
            self._publish(now)
            return

        if entries_globally_blocked() and self.state.position:
            self._exit(self.state.fut_ltp or 0.0, None, "KILL_SWITCH", now)
            self._publish(now)
            return

        self._ensure_future()
        if not self.state.fut_key and not MOCK_MODE:
            self.state.setup_label = "Waiting for Nifty future"
            self._publish(now)
            return

        self._refresh_quotes()

        if not self.state.orb_done:
            if now.time() < ORB_END:
                self.state.setup_label = "Waiting OR 09:18–09:21"
                self._publish(now)
                return
            if not self._lock_orb(now):
                self.state.setup_label = "ORB fetch retry"
                self._publish(now)
                return

        fut_ltp = self.state.fut_ltp
        if fut_ltp is None:
            self._publish(now)
            return

        if self.state.position:
            self._catchup_fanout()
            self._manage(fut_ltp, now)
            self._publish(now)
            return

        if now.time() >= FORCE_EXIT_TIME:
            self.state.setup_label = "No position · past force-exit"
            self._publish(now)
            return

        if self._can_enter(now):
            self._try_entry(fut_ltp, now)

        self._publish(now)

    def _roll_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self.state.trade_day == today:
            return
        if self.state.position:
            self._exit(self.state.fut_ltp or 0.0, None, "DAY_ROLL", now)
        self.state = S29State(trade_day=today)
        self.state.setup_label = "New session"

    def _hydrate(self) -> None:
        raw = cache_manager.get_json(cache_manager.S29_STATE_KEY)
        if not isinstance(raw, dict):
            return
        today = datetime.now(IST).date().isoformat()
        if str(raw.get("trade_day") or "") != today:
            return
        self.state.trade_day = today
        self.state.trades_today = int(raw.get("trades_today") or 0)
        self.state.daily_pnl_inr = float(raw.get("total_daily_pnl_inr") or 0.0)
        self.state.win_lock = bool(raw.get("win_lock") or False)
        self.state.ce_taken = bool(raw.get("ce_taken") or False)
        self.state.pe_taken = bool(raw.get("pe_taken") or False)
        self.state.setup_label = str(raw.get("setup_label") or self.state.setup_label)
        idx = (raw.get("indices") or {}).get(INDEX_CODE) or {}
        if isinstance(idx, dict):
            if idx.get("fut_label"):
                self.state.fut_label = str(idx["fut_label"])
            if idx.get("fut_ltp") is not None:
                self.state.fut_ltp = float(idx["fut_ltp"])
            elif idx.get("spot") is not None:
                self.state.fut_ltp = float(idx["spot"])
            if idx.get("index_spot") is not None:
                self.state.index_spot = float(idx["index_spot"])
            if idx.get("or_high") is not None and idx.get("or_low") is not None:
                self.state.orb_high = float(idx["or_high"])
                self.state.orb_low = float(idx["or_low"])
                self.state.orb_range = abs(self.state.orb_high - self.state.orb_low)
                self.state.orb_done = True
        if self.state.win_lock:
            logger.info("S29 hydrated first-win lock trades=%d", self.state.trades_today)
        elif self.state.trades_today:
            logger.info("S29 hydrated trades_today=%d", self.state.trades_today)

    def _ensure_future(self) -> None:
        if self.state.fut_key:
            return
        if MOCK_MODE:
            self.state.fut_key = "MOCK|NIFTY-FUT"
            self.state.fut_label = "NIFTY FUT"
            return
        if not self.client:
            return
        contract = self.client.get_index_future_contract(INDEX_CODE)
        if not contract or not contract.get("instrument_key"):
            logger.warning("S29 no Nifty future contract yet")
            return
        self.state.fut_key = str(contract["instrument_key"])
        self.state.fut_label = str(contract.get("contract_label") or contract.get("trading_symbol") or "NIFTY FUT")
        logger.info("S29 future %s %s", self.state.fut_label, self.state.fut_key)

    def _lock_orb(self, now: datetime) -> bool:
        bars = self._orb_minute_bars(now)
        if len(bars) < 2:
            logger.warning("S29 ORB not ready — %d 1m bars in 09:18-09:21", len(bars))
            return False
        high = max(float(c["high"]) for c in bars)
        low = min(float(c["low"]) for c in bars)
        rng = high - low
        if rng <= 0:
            return False
        self.state.orb_high = high
        self.state.orb_low = low
        self.state.orb_range = rng
        self.state.orb_done = True
        ce_tr = high + ENTRY_BUFFER
        pe_tr = low - ENTRY_BUFFER
        self.state.setup_label = f"OR locked {high:.0f}/{low:.0f}"
        msg = (
            f"S29 ORB FORMED | HIGH {high:.2f} LOW {low:.2f} RANGE {rng:.2f} | "
            f"CE>{ce_tr:.2f} PE<{pe_tr:.2f} | {self.state.fut_label}"
        )
        self.state.signal_log.append(msg)
        logger.info(msg)
        telegram_notifier.notify_system_event("S29 ORB FORMED", msg)
        return True

    def _orb_minute_bars(self, now: datetime) -> list[dict[str, float]]:
        if MOCK_MODE:
            base = self._mock_fut
            out: list[dict[str, float]] = []
            for i, minute in enumerate((18, 19, 20)):
                ts = datetime.combine(now.date(), dtime(9, minute), tzinfo=IST)
                out.append({
                    "timestamp": ts.isoformat(),
                    "open": base, "high": base + 8 + i, "low": base - 6, "close": base + 2,
                })
            return out
        if not self.client or not self.state.fut_key:
            return []
        key = quote(self.state.fut_key, safe="")
        v3 = self.client.base_url.replace("/v2", "/v3")
        data = self.client._get(f"{v3}/historical-candle/intraday/{key}/minutes/1")  # noqa: SLF001
        raw = data.get("candles") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return []
        bars: list[dict[str, float]] = []
        today = now.date()
        for row in raw:
            candle = _parse_candle_row(row)
            if not candle:
                continue
            ts = datetime.fromisoformat(candle["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            local = ts.astimezone(IST)
            if local.date() != today:
                continue
            t = local.time().replace(second=0, microsecond=0)
            if ORB_START <= t <= ORB_END:
                bars.append(candle)
        bars.sort(key=lambda c: c["timestamp"])
        return bars

    def _fut_ltp(self) -> float | None:
        if MOCK_MODE:
            self._mock_fut += 0.4
            return round(self._mock_fut, 2)
        if not self.client or not self.state.fut_key:
            return None
        return self.client.get_ltp(self.state.fut_key)

    def _refresh_quotes(self) -> None:
        fut = self._fut_ltp()
        if fut is not None:
            self.state.fut_ltp = fut
        if MOCK_MODE:
            self.state.index_spot = round(self._mock_fut - 12.0, 2)
            return
        if not self.client:
            return
        spot = self.client.get_ltp(self.cfg.spot_instrument_key)
        if spot is not None:
            self.state.index_spot = float(spot)

    def _can_enter(self, now: datetime) -> bool:
        if self.state.win_lock:
            self.state.setup_label = "First win — done for today"
            return False
        if now.time() >= NO_NEW_ENTRY_AFTER:
            self.state.setup_label = "No new entry after 15:15"
            return False
        if now.time() < ORB_END:
            return False
        if self.state.trades_today >= MAX_TRADES_PER_DAY:
            self.state.setup_label = "Max trades hit"
            return False
        if self.state.daily_pnl_inr <= DAILY_MAX_LOSS:
            self.state.setup_label = "Daily loss limit"
            return False
        if entries_globally_blocked() or profit_target_engaged():
            self.state.setup_label = "Entries blocked (kill / daily target)"
            return False
        return True

    def _try_entry(self, fut_ltp: float, now: datetime) -> None:
        assert self.state.orb_high is not None and self.state.orb_low is not None
        high = self.state.orb_high
        low = self.state.orb_low
        rng = float(self.state.orb_range or (high - low))
        ce_trigger = high + ENTRY_BUFFER
        pe_trigger = low - ENTRY_BUFFER
        side: str | None = None
        if fut_ltp > ce_trigger and not self.state.ce_taken:
            side = "CE"
        elif fut_ltp < pe_trigger and not self.state.pe_taken:
            side = "PE"
        if side is None:
            self.state.setup_label = (
                f"Watching FUT {fut_ltp:.2f} | CE>{ce_trigger:.0f} PE<{pe_trigger:.0f}"
            )
            return

        direction = "LONG" if side == "CE" else "SHORT"
        if side == "CE":
            sl_fut = low
            tgt_fut = ce_trigger + RANGE_TARGET_MULT * rng
        else:
            sl_fut = high
            tgt_fut = pe_trigger - RANGE_TARGET_MULT * rng

        global_paper = self.paper
        legs = place_s29_entries(
            index_code=INDEX_CODE,
            direction=direction,
            lot_size=self.cfg.lot_size,
            lots=1,
            upstox_market_client=self.client,
            global_paper=global_paper,
            spot=fut_ltp,
        )
        if not legs:
            logger.error("S29 %s entry aborted — no broker orders placed", side)
            return

        primary = next((leg for leg in legs if leg.get("broker") == "upstox"), legs[0])
        qty = int(primary.get("quantity") or self.cfg.lot_size)
        premiums = [
            float(leg["premium_entry"])
            for leg in legs
            if leg.get("premium_entry") is not None
        ]
        premium = max(premiums) if premiums else None
        option_sl = None
        if premium is not None and qty > 0:
            option_sl = round(premium - (MAX_LOSS_PER_TRADE / qty), 2)

        fanout_note = legs_summary(legs)
        pos = S29Position(
            direction=direction,
            option_side=side,
            entry_fut=fut_ltp,
            sl_fut=sl_fut,
            tgt_fut=tgt_fut,
            option_sl=option_sl,
            premium_entry=premium,
            instrument_key=str(primary.get("instrument_key") or ""),
            option_strike=int(primary.get("option_strike") or 0),
            option_type=str(primary.get("option_type") or side),
            lots=int(primary.get("lots") or 1),
            lot_size=self.cfg.lot_size,
            opened_at=now.isoformat(),
            order_legs=legs,
        )
        self.state.position = pos
        self.state.trades_today += 1
        if side == "CE":
            self.state.ce_taken = True
        else:
            self.state.pe_taken = True
        reason = f"S29 {side} breakout FUT {fut_ltp:.2f} vs {ce_trigger if side == 'CE' else pe_trigger:.2f}"
        self.state.signal_log.append(f"{reason} [{fanout_note}]")
        self.state.setup_label = f"S29 {side} @ {fut_ltp:.2f}"
        logger.info("%s [%s]", reason, fanout_note)
        telegram_notifier.notify_trade_execution(
            index_name=f"Nifty 9:18 ORB ({pos.option_strike}{pos.option_type})",
            trade_type=direction,
            entry_price=fut_ltp,
            target_price=tgt_fut,
            sl_price=sl_fut,
            component_sentiment=side,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _manage(self, fut_ltp: float, now: datetime) -> None:
        pos = self.state.position
        if pos is None:
            return
        if now.time() >= FORCE_EXIT_TIME:
            opt = self._option_ltp(pos)
            self._exit(fut_ltp, opt, "FORCE_EXIT_1525", now)
            return

        hit_sl = fut_ltp <= pos.sl_fut if pos.option_side == "CE" else fut_ltp >= pos.sl_fut
        hit_tgt = fut_ltp >= pos.tgt_fut if pos.option_side == "CE" else fut_ltp <= pos.tgt_fut
        opt_ltp = self._option_ltp(pos)
        hit_opt_sl = (
            pos.option_sl is not None
            and opt_ltp is not None
            and opt_ltp <= pos.option_sl
        )
        if hit_tgt:
            self._exit(fut_ltp, opt_ltp, "TARGET HIT", now)
        elif hit_opt_sl:
            self._exit(fut_ltp, opt_ltp, "MAX LOSS HIT", now)
        elif hit_sl:
            self._exit(fut_ltp, opt_ltp, "SL HIT", now)

    def _option_ltp(self, pos: S29Position) -> float | None:
        if MOCK_MODE or not self.client or not pos.instrument_key:
            return pos.premium_entry
        if pos.instrument_key.startswith(("groww:", "kite:")):
            return pos.premium_entry
        return self.client.get_ltp(pos.instrument_key)

    def _exit(self, fut_ltp: float, opt_ltp: float | None, reason: str, now: datetime) -> None:
        pos = self.state.position
        if pos is None:
            return
        place_s29_exits(position_legs(pos), pos.direction, global_paper=self.paper)
        exit_px = float(opt_ltp if opt_ltp is not None else (pos.premium_entry or 0.0))
        entry_px = float(pos.premium_entry or 0.0)
        pnl_pts = exit_px - entry_px if entry_px else 0.0
        qty = max(1, pos.lots * pos.lot_size)
        pnl_inr = pnl_pts * qty
        self.state.daily_pnl_inr += pnl_inr
        self.state.position = None
        won = pnl_inr > 0 or (entry_px <= 0 and (
            (pos.option_side == "CE" and fut_ltp > pos.entry_fut)
            or (pos.option_side == "PE" and fut_ltp < pos.entry_fut)
        ))
        if won:
            self.state.win_lock = True
            self.state.setup_label = f"First win — done for today ({reason})"
        else:
            self.state.setup_label = f"Flat — {reason}"
        self.state.signal_log.append(f"Exit {reason} FUT {fut_ltp:.2f} ({pnl_pts:+.2f} opt)")
        performance_store.record_completed_trade(
            strategy=performance_store.STRATEGY_S29_ORB,
            strategy_id="s29_orb",
            symbol=INDEX_CODE,
            direction=pos.direction,
            entry_price=pos.entry_fut,
            exit_price=fut_ltp,
            pnl_points=pnl_pts,
            exit_reason=reason,
            entry_at=pos.opened_at,
            paper_trading=self.paper,
        )
        logger.info("S29 exit %s FUT %.2f | opt %+.2f | INR %+.0f", reason, fut_ltp, pnl_pts, pnl_inr)
        telegram_notifier.notify_trade_exit(
            index_name=f"Nifty 9:18 ORB ({pos.option_strike}{pos.option_type})",
            trade_type=pos.direction,
            exit_price=fut_ltp,
            pnl_points=pnl_pts,
            reason=reason,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _catchup_fanout(self) -> None:
        pos = self.state.position
        if pos is None or self.paper:
            return
        now_mono = time.monotonic()
        if now_mono - self.state.last_fanout_catchup_mono < 60.0:
            return
        self.state.last_fanout_catchup_mono = now_mono
        existing = list(pos.order_legs or [])
        covered = {
            str(leg.get("username") or "").strip()
            for leg in existing
            if isinstance(leg, dict) and leg.get("username")
        }
        new_legs = catchup_s29_legs(
            index_code=INDEX_CODE,
            direction=pos.direction,
            lot_size=self.cfg.lot_size,
            lots=pos.lots,
            existing_legs=existing,
            upstox_market_client=self.client,
            global_paper=False,
            spot=self.state.fut_ltp or pos.entry_fut,
            exclude_usernames=frozenset(n for n in covered if n),
        )
        if not new_legs:
            return
        pos.order_legs = existing + new_legs
        note = legs_summary(new_legs)
        msg = f"S29 catch-up entry [{note}]"
        self.state.signal_log.append(msg)
        logger.info(msg)

    def _publish(self, now: datetime) -> None:
        s = self.state
        idx: dict[str, Any] = {
            "spot": s.fut_ltp,
            "index_spot": s.index_spot,
            "fut_ltp": s.fut_ltp,
            "or_high": s.orb_high,
            "or_low": s.orb_low,
            "day_review": "ARMED" if s.orb_done else "PENDING",
            "trades_today": s.trades_today,
            "setup_label": s.setup_label,
            "signals": s.signal_log[-5:],
            "fut_label": s.fut_label,
        }
        if s.position:
            idx["position"] = {
                "direction": s.position.direction,
                "entry": s.position.entry_fut,
                "sl": s.position.sl_fut,
                "tp1": s.position.tgt_fut,
                "lots": s.position.lots,
                "legs": legs_summary(s.position.order_legs),
            }
        cache_manager.set_json(
            cache_manager.S29_STATE_KEY,
            {
                "timestamp": now.isoformat(),
                "strategy": performance_store.STRATEGY_S29_ORB,
                "paper_trading": self.paper,
                "total_daily_pnl_inr": round(s.daily_pnl_inr, 2),
                "trade_day": s.trade_day,
                "trades_today": s.trades_today,
                "win_lock": s.win_lock,
                "ce_taken": s.ce_taken,
                "pe_taken": s.pe_taken,
                "indices": {INDEX_CODE: idx},
            },
            ttl_seconds=86_400,
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    S29NiftyOrbEngine().run()


if __name__ == "__main__":
    main()
