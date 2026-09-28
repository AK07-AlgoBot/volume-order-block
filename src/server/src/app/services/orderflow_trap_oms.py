"""OF Trap absorption → ITM option trade (same 5m candle).

S (sell absorbed at low)  → BUY CE
B (buy absorbed at high)  → BUY PE

Risk is taken from the *option* 5m bar that matches the index absorption time:
SL = that candle's low, entry = close, target = entry + 4R.
At +1R premium, SL moves to cost; then SL +1 for each further +1 peak.
Live by default (OFTRAP_LIVE=1). Set OFTRAP_LIVE=0 for paper-only.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from app.services import cache_manager, performance_store, telegram_notifier
from app.services.breakout_order_fanout import (
    catchup_oftrap_legs,
    legs_summary,
    leg_usernames,
    place_oftrap_entries,
    place_oftrap_exits,
    position_legs,
)
from app.services.engine_intraday import entries_globally_blocked, profit_target_engaged
from app.services.upstox_engine import (
    INDEX_CONFIGS,
    MOCK_MODE,
    UpstoxClient,
    build_upstox_client,
)

logger = logging.getLogger("ak07.oftrap.oms")

IST = ZoneInfo("Asia/Kolkata")
RR_TARGET = float(os.environ.get("OFTRAP_RR", "4"))
FORCE_EXIT = dtime(15, 20)
NO_ENTRY_AFTER = dtime(15, 10)
MCX_FORCE_EXIT = dtime(23, 20)
MCX_NO_ENTRY_AFTER = dtime(23, 10)
MCX_SYMBOLS = frozenset({"GOLD", "SILVER", "CRUDE", "CRUDEOIL"})
MIN_RISK = float(os.environ.get("OFTRAP_MIN_OPT_RISK", "1.0"))


def _is_mcx(symbol: str) -> bool:
    return symbol.upper() in MCX_SYMBOLS


def _entry_cutoff(symbol: str) -> dtime:
    return MCX_NO_ENTRY_AFTER if _is_mcx(symbol) else NO_ENTRY_AFTER


def _flat_time(symbol: str) -> tuple[dtime, str]:
    if _is_mcx(symbol):
        return MCX_FORCE_EXIT, "TIME EXIT 23:20"
    return FORCE_EXIT, "TIME EXIT 15:20"


def _hhmm(ts: str) -> str:
    dt = datetime.fromisoformat(str(ts))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).strftime("%H:%M")


def apply_rr_trail(*, entry: float, sl: float, peak: float, r_pts: float, target: float) -> tuple[float, bool]:
    """Return (new_sl, trail_armed). At +1R → cost; then +1 SL per +1 peak."""
    if r_pts <= 0:
        return sl, False
    armed = peak + 1e-9 >= entry + r_pts
    if not armed:
        return sl, False
    extra = max(0, int((peak - entry - r_pts) + 1e-9))
    new_sl = round(entry + extra, 2)
    new_sl = min(new_sl, round(target - 0.05, 2))
    return (new_sl if new_sl > sl + 1e-9 else sl), True


@dataclass
class OfTrapPosition:
    symbol: str
    kind: str
    direction: str
    option_side: str
    bar_time: str
    instrument_key: str
    strike: int
    expiry: str
    entry: float
    sl: float
    target: float
    r_pts: float
    option_ohlc: dict[str, float]
    opened_at: str
    trail_armed: bool = False
    premium_high: float = 0.0
    ltp: float | None = None
    status: str = "open"
    exit_px: float | None = None
    exit_reason: str = ""
    order_legs: list[dict[str, Any]] = field(default_factory=list)
    lots: int = 1
    lot_size: int = 65
    tracked_only: bool = False
    log: list[str] = field(default_factory=list)


class OfTrapOms:
    def __init__(self) -> None:
        self.paper = bool(MOCK_MODE) or os.environ.get("OFTRAP_LIVE", "1") not in ("1", "true", "True")
        self.client: UpstoxClient | None = None
        self.positions: dict[str, OfTrapPosition] = {}
        self.closed: list[dict[str, Any]] = []
        self._last_fanout_catchup_mono = 0.0
        try:
            self._hydrate()
        except Exception:  # noqa: BLE001
            logger.exception("OF Trap hydrate skipped")
        self._publish()
        logger.info("OF Trap OMS %s | MOCK=%s", "PAPER" if self.paper else "LIVE", MOCK_MODE)

    def _client(self) -> UpstoxClient:
        if self.client is None:
            self.client = build_upstox_client()
        return self.client

    def plan_from_absorption(
        self,
        symbol: str,
        kind: str,
        bar_time: str,
        spot: float,
        *,
        client: UpstoxClient | None = None,
        underlying_key: str = "",
    ) -> dict[str, Any]:
        """Resolve ITM option + same-TF candle → entry/SL/1:4. No order."""
        code = "CRUDEOIL" if symbol.upper() in ("CRUDE", "CRUDEOIL") else symbol.upper()
        cfg = INDEX_CONFIGS.get(code)
        if cfg:
            underlying = cfg.spot_instrument_key
            lot_size = int(cfg.lot_size)
        elif underlying_key:
            # MCX: option chain is requested on the subscribed future when there is no index spot.
            underlying = underlying_key
            lot_size = {"GOLD": 100, "SILVER": 30, "CRUDEOIL": 100}.get(code, 1)
        else:
            raise ValueError(f"unsupported symbol {symbol}")
        if kind not in ("S", "B"):
            raise ValueError(f"trade only on S/B absorption, got {kind}")
        direction = "LONG" if kind == "S" else "SHORT"
        cli = client or self._client()
        picked = cli.get_itm_option_contract(underlying, spot, direction)
        if not picked or not picked.get("instrument_key"):
            raise RuntimeError(f"no ITM option for {code} {direction} @ {spot}")
        candles = cli.get_closed_5min_candles(str(picked["instrument_key"])) or []
        opt = next((c for c in candles if _hhmm(c["timestamp"]) == bar_time), None)
        if opt is None:
            raise RuntimeError(
                f"no {bar_time} 5m candle on {picked.get('option_type')}{picked.get('strike')}"
            )
        entry = float(opt["close"])
        sl = float(opt["low"])
        risk = round(entry - sl, 2)
        if risk < MIN_RISK:
            raise RuntimeError(f"option risk {risk} < {MIN_RISK} (C={entry} L={sl})")
        target = round(entry + RR_TARGET * risk, 2)
        return {
            "symbol": symbol.upper(),
            "kind": kind,
            "direction": direction,
            "option_side": str(picked.get("option_type") or ("CE" if direction == "LONG" else "PE")),
            "bar_time": bar_time,
            "spot": spot,
            "instrument_key": str(picked["instrument_key"]),
            "strike": int(picked["strike"]),
            "expiry": str(picked.get("expiry") or ""),
            "delta": picked.get("abs_delta") or picked.get("delta"),
            "selection": picked.get("selection"),
            "entry": entry,
            "sl": sl,
            "target": target,
            "r_pts": risk,
            "rr": RR_TARGET,
            "option_ohlc": {
                "open": float(opt["open"]),
                "high": float(opt["high"]),
                "low": float(opt["low"]),
                "close": float(opt["close"]),
            },
            "paper": self.paper,
            "lot_size": lot_size,
        }

    def on_absorption(
        self,
        symbol: str,
        kind: str,
        bar_time: str,
        spot: float,
        *,
        underlying_key: str = "",
    ) -> OfTrapPosition | None:
        if kind not in ("S", "B"):
            return None
        sym = symbol.upper()
        now = datetime.now(IST)
        if now.time() >= _entry_cutoff(sym):
            logger.info("skip %s %s — past %s", sym, bar_time, _entry_cutoff(sym))
            return None
        held = self.positions.get(sym)
        if held and held.status == "open":
            logger.info("skip %s %s — already in %s %s", sym, bar_time, held.kind, held.bar_time)
            return None
        if entries_globally_blocked() or profit_target_engaged():
            logger.info("skip %s %s — entries blocked (kill / daily target)", symbol, bar_time)
            return None
        try:
            plan = self.plan_from_absorption(
                symbol, kind, bar_time, spot, underlying_key=underlying_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("OF Trap plan failed %s %s %s: %s", symbol, kind, bar_time, exc)
            return None
        lot_size = int(plan.get("lot_size") or 65)
        legs = place_oftrap_entries(
            index_code=plan["symbol"],
            direction=plan["direction"],
            lot_size=lot_size,
            lots=1,
            upstox_market_client=self._client(),
            global_paper=self.paper,
            spot=spot,
        )
        tracked_only = False
        if not legs:
            # No broker fill (commodity fan-out, or live account with no margin).
            # Still track the option LTP so the performance log gets the points.
            tracked_only = not self.paper
            legs = [{
                "username": "AK07",
                "broker": "track",
                "trading_symbol": f"{plan['strike']}{plan['option_side']}",
                "instrument_key": plan["instrument_key"],
                "quantity": lot_size,
                "lots": 1,
                "paper": True,
                "instrument_kind": "options",
                "option_strike": plan["strike"],
                "option_type": plan["option_side"],
            }]
            logger.warning(
                "OF Trap %s %s %s track-only — no broker legs", symbol, kind, bar_time,
            )
        primary = next((leg for leg in legs if leg.get("broker") == "upstox"), legs[0])
        fill = next(
            (float(leg["premium_entry"]) for leg in legs if leg.get("premium_entry") is not None),
            plan["entry"],
        )
        mode = "TRACK" if tracked_only else ("PAPER" if self.paper else "LIVE")
        pos = OfTrapPosition(
            symbol=plan["symbol"],
            kind=kind,
            direction=plan["direction"],
            option_side=plan["option_side"],
            bar_time=bar_time,
            instrument_key=str(primary.get("instrument_key") or plan["instrument_key"]),
            strike=int(primary.get("option_strike") or plan["strike"]),
            expiry=plan["expiry"],
            entry=plan["entry"],
            sl=plan["sl"],
            target=plan["target"],
            r_pts=plan["r_pts"],
            option_ohlc=plan["option_ohlc"],
            opened_at=now.isoformat(),
            premium_high=max(plan["entry"], fill),
            ltp=fill,
            order_legs=legs,
            lots=int(primary.get("lots") or 1),
            lot_size=lot_size,
            tracked_only=tracked_only,
            log=[
                f"{bar_time} {kind} → BUY {plan['option_side']} {plan['strike']} "
                f"entry {plan['entry']:.2f} SL {plan['sl']:.2f} TP {plan['target']:.2f} (1:{RR_TARGET:.0f}) "
                f"[{legs_summary(legs)}]"
            ],
        )
        self.positions[sym] = pos
        self._publish()
        logger.warning(
            "[%s %s] %s BUY %s%d  entry=%.2f SL=%.2f TP=%.2f R=%.2f [%s]",
            symbol, bar_time, mode, pos.option_side, pos.strike, pos.entry, pos.sl, pos.target, pos.r_pts,
            legs_summary(legs),
        )
        try:
            telegram_notifier.notify_trade_execution(
                index_name=f"OF TRAP {kind} {mode} {symbol}",
                trade_type=f"BUY {pos.option_side} {pos.strike}",
                entry_price=pos.entry,
                target_price=pos.target,
                sl_price=pos.sl,
                component_sentiment=f"{legs_summary(legs)}; trail 1R to cost then +1/pt",
                timestamp=bar_time,
                context_label="Legs",
            )
        except Exception:  # noqa: BLE001
            logger.exception("telegram oftrap trade failed")
        return pos

    def poll(self) -> None:
        for sym in list(self.positions):
            pos = self.positions.get(sym)
            if pos is None or pos.status != "open":
                continue
            self._poll_one(pos)

    def _poll_one(self, pos: OfTrapPosition) -> None:
        now = datetime.now(IST)
        try:
            ltp = self._client().get_ltp(pos.instrument_key)
        except Exception:  # noqa: BLE001
            ltp = None
        if ltp is None:
            self._publish()
            return
        pos.ltp = round(float(ltp), 2)
        if ltp > pos.premium_high:
            pos.premium_high = ltp
        new_sl, armed = apply_rr_trail(
            entry=pos.entry, sl=pos.sl, peak=pos.premium_high, r_pts=pos.r_pts, target=pos.target,
        )
        if armed and (not pos.trail_armed or new_sl > pos.sl + 1e-9):
            pos.trail_armed = True
            if new_sl > pos.sl + 1e-9:
                pos.log.append(f"trail SL {pos.sl:.2f} → {new_sl:.2f} (peak {pos.premium_high:.2f})")
                pos.sl = new_sl
        reason = ""
        flat_at, flat_reason = _flat_time(pos.symbol)
        if ltp <= pos.sl:
            reason = "SL" if not pos.trail_armed else ("COST" if abs(pos.sl - pos.entry) < 0.05 else "TRAIL SL")
        elif ltp >= pos.target:
            reason = "TARGET 1:4"
        elif now.time() >= flat_at:
            reason = flat_reason
        if reason:
            if not self._close_position(pos, ltp, reason):
                self._publish()
                return
        else:
            self._catchup_fanout(pos, spot=ltp)
        self._publish()

    def _close_position(self, pos: OfTrapPosition, ltp: float, reason: str) -> bool:
        if not pos.tracked_only and not self.paper:
            ok = place_oftrap_exits(position_legs(pos), pos.direction, global_paper=False)
            if not ok:
                logger.error("OF Trap live exit failed — will retry %s", reason)
                return False
        pos.status = "closed"
        pos.exit_px = ltp
        pos.exit_reason = reason
        pnl = round(ltp - pos.entry, 2)
        pos.log.append(f"exit {reason} @ {ltp:.2f}  Δ {pnl:+.2f}")
        self.closed.append(asdict(pos))
        mode = "TRACK" if pos.tracked_only else ("PAPER" if self.paper else "LIVE")
        logger.warning("[%s %s] %s EXIT %s @ %.2f (entry %.2f)", pos.symbol, pos.bar_time, mode, reason, ltp, pos.entry)
        try:
            performance_store.record_completed_trade(
                strategy=performance_store.STRATEGY_OFTRAP,
                strategy_id="oftrap",
                symbol=pos.symbol,
                direction=pos.direction,
                entry_price=pos.entry,
                exit_price=ltp,
                pnl_points=pnl,
                exit_reason=reason,
                entry_at=pos.opened_at,
                paper_trading=self.paper or pos.tracked_only,
                extra={
                    "participants": leg_usernames(pos.order_legs),
                    "kind": pos.kind,
                    "tracked_only": pos.tracked_only,
                    "option_side": pos.option_side,
                    "option_strike": pos.strike,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("OF Trap performance record failed")
        try:
            telegram_notifier.notify_trade_exit(
                index_name=f"OF TRAP {pos.kind} {mode} {pos.symbol}",
                trade_type=f"{pos.option_side} {pos.strike}",
                exit_price=ltp,
                pnl_points=pnl,
                reason=f"{reason} | {legs_summary(pos.order_legs)}",
                timestamp=pos.bar_time,
            )
        except Exception:  # noqa: BLE001
            logger.exception("telegram oftrap exit failed")
        self.positions.pop(pos.symbol, None)
        return True

    def _catchup_fanout(self, pos: OfTrapPosition, *, spot: float | None) -> None:
        if pos.tracked_only or self.paper or pos.status != "open":
            return
        now_mono = time.monotonic()
        if now_mono - self._last_fanout_catchup_mono < 60.0:
            return
        self._last_fanout_catchup_mono = now_mono
        existing = list(pos.order_legs or [])
        covered = {
            str(leg.get("username") or "").strip()
            for leg in existing
            if isinstance(leg, dict) and leg.get("username")
        }
        new_legs = catchup_oftrap_legs(
            index_code=pos.symbol,
            direction=pos.direction,
            lot_size=pos.lot_size,
            lots=pos.lots,
            existing_legs=existing,
            upstox_market_client=self._client(),
            global_paper=False,
            spot=spot or pos.entry,
            exclude_usernames=frozenset(n for n in covered if n),
        )
        if not new_legs:
            return
        pos.order_legs = existing + new_legs
        pos.log.append(f"catch-up [{legs_summary(new_legs)}]")
        logger.info("OF Trap catch-up [%s]", legs_summary(new_legs))

    def _hydrate(self) -> None:
        raw = cache_manager.get_json(cache_manager.OFTRAP_TRADE_KEY)
        if not isinstance(raw, dict):
            return
        closed = raw.get("closed")
        if isinstance(closed, list):
            self.closed = [c for c in closed if isinstance(c, dict)][-20:]
        stored_paper = bool(raw.get("paper"))
        blobs: list[dict[str, Any]] = []
        stored = raw.get("positions")
        if isinstance(stored, dict):
            blobs.extend(v for v in stored.values() if isinstance(v, dict))
        elif isinstance(raw.get("position"), dict):
            blobs.append(raw["position"])
        if stored_paper != self.paper and blobs:
            logger.warning(
                "OF Trap drop leftover %s position(s) — OMS is now %s",
                "PAPER" if stored_paper else "LIVE",
                "PAPER" if self.paper else "LIVE",
            )
            return
        for pos_raw in blobs:
            if pos_raw.get("status") != "open":
                continue
            try:
                pos = self._position_from_raw(pos_raw)
            except (TypeError, ValueError, KeyError):
                logger.exception("OF Trap failed to hydrate position")
                continue
            self.positions[pos.symbol] = pos
            logger.info(
                "OF Trap hydrated %s %s %s %s%d",
                pos.symbol, pos.kind, pos.bar_time, pos.option_side, pos.strike,
            )

    @staticmethod
    def _position_from_raw(pos_raw: dict[str, Any]) -> OfTrapPosition:
        return OfTrapPosition(
            symbol=str(pos_raw.get("symbol") or "NIFTY"),
            kind=str(pos_raw.get("kind") or "S"),
            direction=str(pos_raw.get("direction") or "LONG"),
            option_side=str(pos_raw.get("option_side") or "CE"),
            bar_time=str(pos_raw.get("bar_time") or ""),
            instrument_key=str(pos_raw.get("instrument_key") or ""),
            strike=int(pos_raw.get("strike") or 0),
            expiry=str(pos_raw.get("expiry") or ""),
            entry=float(pos_raw.get("entry") or 0.0),
            sl=float(pos_raw.get("sl") or 0.0),
            target=float(pos_raw.get("target") or 0.0),
            r_pts=float(pos_raw.get("r_pts") or 0.0),
            option_ohlc=dict(pos_raw.get("option_ohlc") or {}),
            opened_at=str(pos_raw.get("opened_at") or ""),
            trail_armed=bool(pos_raw.get("trail_armed") or False),
            premium_high=float(pos_raw.get("premium_high") or pos_raw.get("entry") or 0.0),
            ltp=float(pos_raw["ltp"]) if pos_raw.get("ltp") is not None else None,
            status="open",
            order_legs=list(pos_raw.get("order_legs") or []),
            lots=int(pos_raw.get("lots") or 1),
            lot_size=int(pos_raw.get("lot_size") or 65),
            tracked_only=bool(pos_raw.get("tracked_only")),
            log=list(pos_raw.get("log") or []),
        )

    def _publish(self) -> None:
        open_pos = {sym: asdict(pos) for sym, pos in self.positions.items() if pos.status == "open"}
        first = next(iter(open_pos.values()), None)
        payload = {
            "paper": self.paper,
            "mode": "PAPER" if self.paper else "LIVE",
            "updated": datetime.now(IST).isoformat(),
            "position": first,
            "positions": open_pos,
            "closed": self.closed[-20:],
        }
        try:
            cache_manager.set_json(cache_manager.OFTRAP_TRADE_KEY, payload, ttl_seconds=86_400)
        except Exception:  # noqa: BLE001
            pass
