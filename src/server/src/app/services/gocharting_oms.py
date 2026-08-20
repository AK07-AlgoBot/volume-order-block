"""GoCharting orderflow OMS — webhook alerts → S3 ITM options + exits.

GoCharting is signal-only (BUY/SELL + ticker). Option strike and futures
price/SL/trail all come from AK07 Upstox data. Alerts are queued by
``/api/gocharting/alert``. This process pops them, fans orders, then
manages futures SL (last closed 5m candle extreme ±5, 1R then 1:1 trail)
and 14:55 IST square-off. No option-premium target.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Final
from zoneinfo import ZoneInfo

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.constants import GC_OF_INDICES
from app.services import cache_manager, performance_store, telegram_notifier
from app.services.breakout_order_fanout import (
    catchup_gc_legs,
    legs_summary,
    place_gc_entries,
    place_gc_exits,
    position_legs,
)
from app.services.engine_intraday import entries_globally_blocked, profit_target_engaged
from app.services.upstox_engine import (
    INDEX_CONFIGS,
    MOCK_MODE,
    UpstoxClient,
    build_upstox_client,
)

logger = logging.getLogger("ak07.gocharting")

IST: Final = ZoneInfo("Asia/Kolkata")
ALLOWED_INDICES: Final[frozenset[str]] = frozenset(GC_OF_INDICES)

NO_NEW_ENTRY_AFTER: Final[dtime] = dtime(14, 45)
FORCE_EXIT_TIME: Final[dtime] = dtime(14, 55)
BOT_END_TIME: Final[dtime] = dtime(15, 5)
SESSION_START: Final[dtime] = dtime(9, 15)
MAX_TRADES_PER_DAY: Final[int] = int(os.environ.get("AK07_GC_MAX_TRADES_PER_DAY", "2"))
DAILY_MAX_LOSS: Final[float] = float(os.environ.get("AK07_GC_DAILY_MAX_LOSS", "-4000"))
SL_BUFFER_PTS: Final[float] = float(os.environ.get("AK07_GC_SL_BUFFER_PTS", "5"))
POLL_SECONDS: Final[float] = float(os.environ.get("AK07_GC_POLL_SECONDS", "3"))
DEDUP_SECONDS: Final[float] = float(os.environ.get("AK07_GC_DEDUP_SECONDS", "90"))

_STRATEGY_ALIASES: Final[dict[str, str]] = {
    "DD": "DD",
    "FD": "FD",
    "OB": "OB",
    "OF": "OF",
    "ST": "ST",
    "STK": "ST",
    "STACK": "ST",
    "SWEEP": "ST",
    "TR": "ST",
    "SW": "SW",
}

_PIPE_RE = re.compile(
    r"^AK07\|(?P<strategy>[A-Z]+)\|(?P<side>BUY|SELL|SIGNAL)"
    r"(?:\|(?P<ticker>[^|]*))?(?:\|(?P<interval>[^|]*))?(?:\|(?P<rest>.*))?$",
    re.IGNORECASE,
)
_SPACE_RE = re.compile(
    r"^AK07\s+(?P<strategy>DD|FD|OB|STK|ST|SW|SWEEP|TR|OF|STACK)\s+(?P<side>BUY|SELL)$",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}")
_FALLBACK_INDEX: Final[str] = "NIFTY"


@dataclass
class GcAlert:
    strategy: str
    side: str  # BUY / SELL
    ticker: str
    index_code: str
    interval: str
    sl: float | None
    raw: str

    @property
    def direction(self) -> str:
        return "LONG" if self.side == "BUY" else "SHORT"

    @property
    def option_side(self) -> str:
        return "CE" if self.side == "BUY" else "PE"

    @property
    def fingerprint(self) -> str:
        return f"{self.index_code}|{self.strategy}|{self.side}"


@dataclass
class GcPosition:
    index_code: str
    direction: str
    option_side: str
    strategy: str
    entry_fut: float
    sl_fut: float | None
    option_sl: float | None
    premium_entry: float | None
    instrument_key: str
    option_strike: int
    option_type: str
    lots: int
    lot_size: int
    opened_at: str
    order_legs: list[dict[str, Any]] = field(default_factory=list)
    initial_sl_fut: float | None = None
    r_pts: float = 0.0
    trail_armed: bool = False
    extreme_fut: float | None = None


@dataclass
class GcState:
    trade_day: str = ""
    trades_today: int = 0
    daily_pnl_inr: float = 0.0
    win_lock: bool = False
    position: GcPosition | None = None
    setup_label: str = "Waiting for GoCharting alert"
    signal_log: list[str] = field(default_factory=list)
    last_alert: str = ""
    last_fingerprint: str = ""
    last_alert_mono: float = 0.0
    last_fanout_catchup_mono: float = 0.0
    fut_keys: dict[str, str] = field(default_factory=dict)
    fut_labels: dict[str, str] = field(default_factory=dict)
    fut_ltp: dict[str, float] = field(default_factory=dict)


def _clean_placeholder(value: str) -> str:
    """GoCharting does not expand TradingView {{ticker}} tokens — drop them."""
    return _PLACEHOLDER_RE.sub("", value or "").strip("|- ")


def parse_index_from_ticker(ticker: str) -> str | None:
    raw = _clean_placeholder(ticker).upper().replace(" ", "")
    if not raw:
        return None
    if "BANKNIFTY" in raw or "NIFTYBANK" in raw:
        return "BANKNIFTY"
    if "FINNIFTY" in raw or "MIDCP" in raw or "SENSEX" in raw:
        return None
    if "NIFTY" in raw:
        return "NIFTY"
    return None


def _resolve_index(ticker: str, default_index: str = "") -> str | None:
    for candidate in (
        ticker,
        default_index,
        os.environ.get("AK07_GC_DEFAULT_INDEX") or "",
    ):
        code = parse_index_from_ticker(str(candidate or ""))
        if code:
            return code
    return None


def _parse_sl(blob: str) -> float | None:
    if not blob:
        return None
    match = re.search(r"(?:^|[|,;\s])sl\s*=\s*([0-9]+(?:\.[0-9]+)?)", blob, re.IGNORECASE)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


def _buffered_sl(alert: GcAlert) -> float | None:
    """Candle extreme ±5 index points — extra room beyond the signal bar."""
    if alert.sl is None or alert.sl <= 0:
        return None
    if alert.option_side == "CE":
        return round(alert.sl - SL_BUFFER_PTS, 2)
    return round(alert.sl + SL_BUFFER_PTS, 2)


def _apply_fut_trail(pos: GcPosition, fut_ltp: float) -> str | None:
    """After 1R, trail SL 1:1 with each further point of favorable futures move.

    Returns a short log line when SL is armed or tightened; otherwise None.
    """
    r_pts = float(pos.r_pts or 0.0)
    if r_pts <= 0 or pos.sl_fut is None:
        return None
    note: str | None = None
    if pos.option_side == "CE":
        extreme = max(float(pos.extreme_fut or fut_ltp), fut_ltp)
        pos.extreme_fut = extreme
        if not pos.trail_armed and extreme >= pos.entry_fut + r_pts:
            pos.trail_armed = True
            note = f"1R trail on @ {extreme:.2f}"
        if pos.trail_armed:
            new_sl = round(extreme - r_pts, 2)
            if new_sl > pos.sl_fut + 1e-9:
                pos.sl_fut = new_sl
                note = f"trail SL {new_sl:.2f} (ext {extreme:.2f})"
        return note
    extreme = min(float(pos.extreme_fut if pos.extreme_fut is not None else fut_ltp), fut_ltp)
    pos.extreme_fut = extreme
    if not pos.trail_armed and extreme <= pos.entry_fut - r_pts:
        pos.trail_armed = True
        note = f"1R trail on @ {extreme:.2f}"
    if pos.trail_armed:
        new_sl = round(extreme + r_pts, 2)
        if new_sl < pos.sl_fut - 1e-9:
            pos.sl_fut = new_sl
            note = f"trail SL {new_sl:.2f} (ext {extreme:.2f})"
    return note


def parse_gocharting_alert(raw: str, *, default_index: str = "") -> GcAlert | None:
    """Parse GoCharting plain-text (or JSON-wrapped) alert body."""
    text = (raw or "").strip()
    json_ticker = ""
    if not text:
        return None
    if text.startswith("{") or text.startswith("["):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(obj, dict):
            json_ticker = str(
                obj.get("ticker") or obj.get("symbol") or obj.get("instrument") or obj.get("market") or ""
            ).strip()
            nested = obj.get("message") or obj.get("text") or obj.get("alert_message") or obj.get("body")
            if nested:
                parsed = parse_gocharting_alert(
                    str(nested),
                    default_index=default_index or json_ticker,
                )
                if parsed:
                    return parsed
            strategy = str(obj.get("strategy") or obj.get("tag") or "").strip().upper()
            side = str(obj.get("side") or obj.get("action") or obj.get("signal") or "").strip().upper()
            ticker = json_ticker
            if strategy and side in ("BUY", "SELL"):
                text = f"AK07|{strategy}|{side}|{ticker}|{obj.get('interval') or ''}|sl={obj.get('sl') or ''}"
            else:
                return None
        else:
            return None

    first_line = text.splitlines()[0].strip()
    match = _PIPE_RE.match(first_line) or _SPACE_RE.match(first_line)
    if not match:
        return None
    groups = match.groupdict()
    strategy_raw = str(groups.get("strategy") or "").upper()
    side = str(groups.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        return None
    strategy = _STRATEGY_ALIASES.get(strategy_raw)
    if not strategy:
        return None
    ticker = _clean_placeholder(str(groups.get("ticker") or "") if "ticker" in groups else "")
    interval = _clean_placeholder(str(groups.get("interval") or "") if "interval" in groups else "")
    rest = str(groups.get("rest") or "") if "rest" in groups else ""
    sl = _parse_sl(_clean_placeholder(rest)) or _parse_sl(_clean_placeholder(first_line))
    index_code = _resolve_index(ticker or json_ticker, default_index)
    if index_code is None:
        index_code = _FALLBACK_INDEX
        logger.warning(
            "GC ticker missing (%r) — using %s. BankNifty alerts need NSE:BANKNIFTY-I or ?index=BANKNIFTY",
            first_line[:180],
            index_code,
        )
    if index_code not in ALLOWED_INDICES:
        return None
    return GcAlert(
        strategy=strategy,
        side=side,
        ticker=ticker or index_code,
        index_code=index_code,
        interval=interval,
        sl=sl,
        raw=first_line,
    )


def enqueue_gocharting_alert(raw: str, *, default_index: str = "") -> dict[str, Any]:
    """Parse + queue an alert. Used by the FastAPI webhook."""
    parsed = parse_gocharting_alert(raw, default_index=default_index)
    payload = {
        "raw": (raw or "")[:2000],
        "received_at": datetime.now(IST).isoformat(),
        "parsed": asdict(parsed) if parsed else None,
    }
    if not parsed:
        return {"ok": False, "ignored": True, "reason": "unrecognized alert"}
    if not cache_manager.rpush_json(cache_manager.GC_ALERT_QUEUE_KEY, payload):
        return {"ok": False, "reason": "redis queue failed"}
    return {
        "ok": True,
        "queued": True,
        "strategy": parsed.strategy,
        "side": parsed.side,
        "index": parsed.index_code,
        "sl": parsed.sl,
    }


class GoChartingOmsEngine:
    def __init__(self) -> None:
        self.paper = bool(MOCK_MODE)
        self.client: UpstoxClient | None = None if MOCK_MODE else build_upstox_client()
        self.state = GcState()
        self._hydrate()
        logger.info(
            "GC orderflow live | paper=%s mock=%s | ITM fan-out | max %d/day | poll=%.0fs",
            self.paper,
            MOCK_MODE,
            MAX_TRADES_PER_DAY,
            POLL_SECONDS,
        )

    def run(self) -> None:
        while True:
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception as exc:
                logger.exception("GC tick error: %s", exc)
            time.sleep(max(0.5, POLL_SECONDS - (time.monotonic() - t0)))

    def tick(self) -> None:
        now = datetime.now(IST)
        if self.client:
            self.client.refresh_access_token_from_disk()
        self._roll_day(now)

        if now.time() >= BOT_END_TIME and self.state.position is None:
            self.state.setup_label = "Session over"
            self._drain_ignored("session over")
            self._publish(now)
            return

        if entries_globally_blocked() and self.state.position:
            idx = self.state.position.index_code
            self._exit(self.state.fut_ltp.get(idx) or 0.0, None, "KILL_SWITCH", now)
            self._publish(now)
            return

        self._refresh_quotes()
        self._process_queue(now)

        pos = self.state.position
        if pos:
            fut_ltp = self.state.fut_ltp.get(pos.index_code)
            if fut_ltp is not None:
                self._catchup_fanout()
                self._manage(fut_ltp, now)

        self._publish(now)

    def _hydrate(self) -> None:
        raw = cache_manager.get_json(cache_manager.GC_STATE_KEY)
        if not isinstance(raw, dict):
            return
        today = datetime.now(IST).date().isoformat()
        if str(raw.get("trade_day") or "") != today:
            return
        self.state.trade_day = today
        self.state.trades_today = int(raw.get("trades_today") or 0)
        self.state.daily_pnl_inr = float(raw.get("total_daily_pnl_inr") or 0.0)
        self.state.win_lock = bool(raw.get("win_lock") or False)
        self.state.setup_label = str(raw.get("setup_label") or self.state.setup_label)
        pos_raw = raw.get("open_position")
        if isinstance(pos_raw, dict) and pos_raw.get("direction"):
            try:
                self.state.position = GcPosition(
                    index_code=str(pos_raw.get("index_code") or "NIFTY"),
                    direction=str(pos_raw["direction"]),
                    option_side=str(pos_raw.get("option_side") or "CE"),
                    strategy=str(pos_raw.get("strategy") or "DD"),
                    entry_fut=float(pos_raw.get("entry_fut") or 0.0),
                    sl_fut=float(pos_raw["sl_fut"]) if pos_raw.get("sl_fut") is not None else None,
                    option_sl=float(pos_raw["option_sl"]) if pos_raw.get("option_sl") is not None else None,
                    premium_entry=float(pos_raw["premium_entry"]) if pos_raw.get("premium_entry") is not None else None,
                    instrument_key=str(pos_raw.get("instrument_key") or ""),
                    option_strike=int(pos_raw.get("option_strike") or 0),
                    option_type=str(pos_raw.get("option_type") or ""),
                    lots=int(pos_raw.get("lots") or 1),
                    lot_size=int(pos_raw.get("lot_size") or 65),
                    opened_at=str(pos_raw.get("opened_at") or ""),
                    order_legs=list(pos_raw.get("order_legs") or []),
                    initial_sl_fut=(
                        float(pos_raw["initial_sl_fut"])
                        if pos_raw.get("initial_sl_fut") is not None
                        else (float(pos_raw["sl_fut"]) if pos_raw.get("sl_fut") is not None else None)
                    ),
                    r_pts=float(pos_raw.get("r_pts") or 0.0),
                    trail_armed=bool(pos_raw.get("trail_armed") or False),
                    extreme_fut=(
                        float(pos_raw["extreme_fut"]) if pos_raw.get("extreme_fut") is not None else None
                    ),
                )
                if self.state.position.r_pts <= 0 and self.state.position.sl_fut is not None:
                    self.state.position.r_pts = abs(
                        self.state.position.entry_fut - self.state.position.sl_fut
                    )
                if self.state.position.extreme_fut is None:
                    self.state.position.extreme_fut = self.state.position.entry_fut
                logger.info("GC hydrated open %s %s", self.state.position.index_code, self.state.position.direction)
            except (TypeError, ValueError, KeyError):
                logger.exception("GC failed to hydrate position")

    def _roll_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self.state.trade_day == today:
            return
        if self.state.position:
            idx = self.state.position.index_code
            self._exit(self.state.fut_ltp.get(idx) or 0.0, None, "DAY_ROLL", now)
        self.state = GcState(trade_day=today)
        self.state.setup_label = "New session"

    def _ensure_future(self, index_code: str) -> str:
        if index_code in self.state.fut_keys:
            return self.state.fut_keys[index_code]
        if MOCK_MODE:
            self.state.fut_keys[index_code] = f"MOCK|{index_code}-FUT"
            self.state.fut_labels[index_code] = f"{index_code} FUT"
            return self.state.fut_keys[index_code]
        if not self.client:
            return ""
        contract = self.client.get_index_future_contract(index_code)
        if not contract or not contract.get("instrument_key"):
            logger.warning("GC no %s future contract yet", index_code)
            return ""
        key = str(contract["instrument_key"])
        self.state.fut_keys[index_code] = key
        self.state.fut_labels[index_code] = str(
            contract.get("contract_label") or contract.get("trading_symbol") or f"{index_code} FUT"
        )
        logger.info("GC future %s %s", self.state.fut_labels[index_code], key)
        return key

    def _refresh_quotes(self) -> None:
        needed = set(ALLOWED_INDICES)
        if self.state.position:
            needed.add(self.state.position.index_code)
        for code in needed:
            key = self._ensure_future(code)
            if MOCK_MODE:
                self.state.fut_ltp[code] = self.state.fut_ltp.get(code, 24_200.0) + 0.2
                continue
            if not self.client or not key:
                continue
            ltp = self.client.get_ltp(key)
            if ltp is not None:
                self.state.fut_ltp[code] = float(ltp)

    def _upstox_signal_sl(self, index_code: str, option_side: str) -> float | None:
        """SL from AK07 Upstox last closed 5m futures candle, not GoCharting prices."""
        if MOCK_MODE:
            ltp = self.state.fut_ltp.get(index_code)
            if ltp is None:
                return None
            if option_side == "CE":
                return round(ltp - 20.0 - SL_BUFFER_PTS, 2)
            return round(ltp + 20.0 + SL_BUFFER_PTS, 2)
        key = self._ensure_future(index_code)
        if not self.client or not key:
            return None
        candles = self.client.get_closed_5min_candles(key)
        if not candles:
            return None
        bar = candles[-1]
        if option_side == "CE":
            return round(float(bar["low"]) - SL_BUFFER_PTS, 2)
        return round(float(bar["high"]) + SL_BUFFER_PTS, 2)

    def _drain_ignored(self, reason: str) -> None:
        while True:
            item = cache_manager.lpop_json(cache_manager.GC_ALERT_QUEUE_KEY)
            if item is None:
                return
            logger.info("GC dropped queued alert (%s)", reason)

    def _process_queue(self, now: datetime) -> None:
        while True:
            item = cache_manager.lpop_json(cache_manager.GC_ALERT_QUEUE_KEY)
            if item is None:
                return
            parsed_raw = item.get("parsed") if isinstance(item, dict) else None
            alert: GcAlert | None = None
            if isinstance(parsed_raw, dict):
                try:
                    alert = GcAlert(
                        strategy=str(parsed_raw.get("strategy") or "DD"),
                        side=str(parsed_raw.get("side") or ""),
                        ticker=str(parsed_raw.get("ticker") or ""),
                        index_code=str(parsed_raw.get("index_code") or "NIFTY"),
                        interval=str(parsed_raw.get("interval") or ""),
                        sl=float(parsed_raw["sl"]) if parsed_raw.get("sl") is not None else None,
                        raw=str(parsed_raw.get("raw") or ""),
                    )
                except (TypeError, ValueError):
                    alert = None
            if alert is None or alert.side not in ("BUY", "SELL"):
                raw = str((item or {}).get("raw") or "") if isinstance(item, dict) else ""
                alert = parse_gocharting_alert(raw)
            if alert is None:
                logger.warning("GC skipped unparsed queue item")
                continue
            self._handle_alert(alert, now)
            if self.state.position:
                return

    def _can_enter(self, now: datetime) -> str | None:
        if now.time() < SESSION_START:
            return "Before session"
        if now.time() >= NO_NEW_ENTRY_AFTER:
            return "No new entry after 14:45"
        if self.state.position is not None:
            return "Already in a trade"
        if self.state.win_lock:
            return "First win — done for today"
        if self.state.trades_today >= MAX_TRADES_PER_DAY:
            return "Max trades hit"
        if self.state.daily_pnl_inr <= DAILY_MAX_LOSS:
            return "Daily loss limit"
        if entries_globally_blocked() or profit_target_engaged():
            return "Entries blocked (kill / daily target)"
        return None

    def _handle_alert(self, alert: GcAlert, now: datetime) -> None:
        self.state.last_alert = alert.raw
        blocked = self._can_enter(now)
        if blocked:
            self.state.setup_label = blocked
            logger.info("GC ignore %s %s %s — %s", alert.strategy, alert.side, alert.index_code, blocked)
            return
        elapsed = time.monotonic() - self.state.last_alert_mono
        if alert.fingerprint == self.state.last_fingerprint and elapsed < DEDUP_SECONDS:
            logger.info("GC dedup %s", alert.fingerprint)
            return

        fut_ltp = self.state.fut_ltp.get(alert.index_code)
        if fut_ltp is None and not MOCK_MODE:
            self._ensure_future(alert.index_code)
            self._refresh_quotes()
            fut_ltp = self.state.fut_ltp.get(alert.index_code)
        if fut_ltp is None:
            logger.error("GC %s entry aborted — no FUT LTP", alert.index_code)
            return

        sl_fut = self._upstox_signal_sl(alert.index_code, alert.option_side)
        if sl_fut is None:
            sl_fut = _buffered_sl(alert)
        if sl_fut is None:
            logger.error("GC %s %s entry aborted — no Upstox 5m SL", alert.index_code, alert.side)
            return

        cfg = INDEX_CONFIGS[alert.index_code]
        legs = place_gc_entries(
            index_code=alert.index_code,
            direction=alert.direction,
            lot_size=cfg.lot_size,
            lots=1,
            upstox_market_client=self.client,
            global_paper=self.paper,
            spot=fut_ltp,
        )
        if not legs:
            logger.error("GC %s %s entry aborted — no broker orders", alert.index_code, alert.option_side)
            return

        primary = next((leg for leg in legs if leg.get("broker") == "upstox"), legs[0])
        premiums = [
            float(leg["premium_entry"])
            for leg in legs
            if leg.get("premium_entry") is not None
        ]
        premium = max(premiums) if premiums else None
        r_pts = abs(fut_ltp - sl_fut)

        pos = GcPosition(
            index_code=alert.index_code,
            direction=alert.direction,
            option_side=alert.option_side,
            strategy=alert.strategy,
            entry_fut=fut_ltp,
            sl_fut=sl_fut,
            option_sl=None,
            premium_entry=premium,
            instrument_key=str(primary.get("instrument_key") or ""),
            option_strike=int(primary.get("option_strike") or 0),
            option_type=str(primary.get("option_type") or alert.option_side),
            lots=int(primary.get("lots") or 1),
            lot_size=cfg.lot_size,
            opened_at=now.isoformat(),
            order_legs=legs,
            initial_sl_fut=sl_fut,
            r_pts=r_pts,
            trail_armed=False,
            extreme_fut=fut_ltp,
        )
        self.state.position = pos
        self.state.trades_today += 1
        self.state.last_fingerprint = alert.fingerprint
        self.state.last_alert_mono = time.monotonic()
        fanout_note = legs_summary(legs)
        sl_txt = f"{sl_fut:.2f}"
        reason = (
            f"GC {alert.strategy} {alert.option_side} {alert.index_code} "
            f"FUT {fut_ltp:.2f} SL {sl_txt} 1R={r_pts:.1f}"
        )
        self.state.signal_log.append(f"{reason} [{fanout_note}]")
        self.state.signal_log = self.state.signal_log[-20:]
        self.state.setup_label = f"GC {alert.strategy} {alert.option_side} @ {fut_ltp:.2f}"
        logger.info("%s [%s]", reason, fanout_note)
        telegram_notifier.notify_trade_execution(
            index_name=f"GC {alert.strategy} {alert.index_code} ({pos.option_strike}{pos.option_type})",
            trade_type=alert.direction,
            entry_price=fut_ltp,
            target_price=(fut_ltp + r_pts) if alert.option_side == "CE" else (fut_ltp - r_pts),
            sl_price=sl_fut or 0.0,
            component_sentiment=alert.option_side,
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    def _manage(self, fut_ltp: float, now: datetime) -> None:
        pos = self.state.position
        if pos is None:
            return
        if now.time() >= FORCE_EXIT_TIME:
            self._exit(fut_ltp, self._option_ltp(pos), "FORCE_EXIT_1455", now)
            return

        trail_note = _apply_fut_trail(pos, fut_ltp)
        if trail_note:
            self.state.signal_log.append(trail_note)
            self.state.signal_log = self.state.signal_log[-20:]
            if pos.trail_armed:
                self.state.setup_label = f"GC {pos.strategy} {pos.option_side} trail SL {pos.sl_fut:.2f}"
            logger.info("GC %s %s", pos.index_code, trail_note)

        hit_sl = False
        if pos.sl_fut is not None:
            if pos.option_side == "CE":
                hit_sl = fut_ltp <= pos.sl_fut
            else:
                hit_sl = fut_ltp >= pos.sl_fut
        if hit_sl:
            self._exit(fut_ltp, self._option_ltp(pos), "SL HIT", now)

    def _option_ltp(self, pos: GcPosition) -> float | None:
        if MOCK_MODE or not self.client or not pos.instrument_key:
            return pos.premium_entry
        if pos.instrument_key.startswith(("groww:", "kite:")):
            return pos.premium_entry
        return self.client.get_ltp(pos.instrument_key)

    def _exit(self, fut_ltp: float, opt_ltp: float | None, reason: str, now: datetime) -> None:
        pos = self.state.position
        if pos is None:
            return
        place_gc_exits(position_legs(pos), pos.direction, global_paper=self.paper)
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
        self.state.signal_log = self.state.signal_log[-20:]
        performance_store.record_completed_trade(
            strategy=performance_store.STRATEGY_GC_OF,
            strategy_id="gc_of",
            symbol=pos.index_code,
            direction=pos.direction,
            entry_price=pos.entry_fut,
            exit_price=fut_ltp,
            pnl_points=pnl_pts,
            exit_reason=reason,
            entry_at=pos.opened_at,
            paper_trading=self.paper,
        )
        logger.info("GC exit %s FUT %.2f | opt %+.2f | INR %+.0f", reason, fut_ltp, pnl_pts, pnl_inr)
        telegram_notifier.notify_trade_exit(
            index_name=f"GC {pos.strategy} {pos.index_code} ({pos.option_strike}{pos.option_type})",
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
        cfg = INDEX_CONFIGS[pos.index_code]
        new_legs = catchup_gc_legs(
            index_code=pos.index_code,
            direction=pos.direction,
            lot_size=cfg.lot_size,
            lots=pos.lots,
            existing_legs=existing,
            upstox_market_client=self.client,
            global_paper=False,
            spot=self.state.fut_ltp.get(pos.index_code) or pos.entry_fut,
            exclude_usernames=frozenset(n for n in covered if n),
        )
        if not new_legs:
            return
        pos.order_legs = existing + new_legs
        note = legs_summary(new_legs)
        msg = f"GC catch-up entry [{note}]"
        self.state.signal_log.append(msg)
        logger.info(msg)

    def _publish(self, now: datetime) -> None:
        s = self.state
        indices: dict[str, Any] = {}
        for code in ALLOWED_INDICES:
            idx: dict[str, Any] = {
                "spot": s.fut_ltp.get(code),
                "or_high": None,
                "or_low": None,
                "day_review": s.setup_label,
                "trades_today": s.trades_today,
                "setup_label": s.setup_label,
                "signals": s.signal_log[-5:],
                "fut_label": s.fut_labels.get(code, ""),
                "last_alert": s.last_alert,
            }
            if s.position and s.position.index_code == code:
                r_pts = float(s.position.r_pts or 0.0)
                one_r = None
                if r_pts > 0:
                    if s.position.option_side == "CE":
                        one_r = round(s.position.entry_fut + r_pts, 2)
                    else:
                        one_r = round(s.position.entry_fut - r_pts, 2)
                idx["or_high"] = s.position.sl_fut
                idx["or_low"] = s.position.sl_fut
                idx["position"] = {
                    "direction": s.position.direction,
                    "entry": s.position.entry_fut,
                    "sl": s.position.sl_fut,
                    "tp1": one_r,
                    "r": s.position.r_pts,
                    "trail": "1R" if s.position.trail_armed else "wait",
                    "lots": s.position.lots,
                    "legs": legs_summary(s.position.order_legs),
                    "strategy": s.position.strategy,
                }
            indices[code] = idx

        pos_dump = None
        if s.position:
            pos_dump = asdict(s.position)
        cache_manager.set_json(
            cache_manager.GC_STATE_KEY,
            {
                "timestamp": now.isoformat(),
                "strategy": performance_store.STRATEGY_GC_OF,
                "paper_trading": self.paper,
                "total_daily_pnl_inr": round(s.daily_pnl_inr, 2),
                "trade_day": s.trade_day,
                "trades_today": s.trades_today,
                "win_lock": s.win_lock,
                "setup_label": s.setup_label,
                "last_alert": s.last_alert,
                "open_position": pos_dump,
                "indices": indices,
            },
            ttl_seconds=86_400,
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    GoChartingOmsEngine().run()


if __name__ == "__main__":
    main()
