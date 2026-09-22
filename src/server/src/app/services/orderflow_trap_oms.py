"""OF Trap absorption → ITM option paper trade (same 5m candle).

S (sell absorbed at low)  → BUY CE
B (buy absorbed at high)  → BUY PE

Risk is taken from the *option* 5m bar that matches the index absorption time:
SL = that candle's low, entry = close, target = entry + 4R.
At +1R premium, SL moves to cost; then SL +1 for each further +1 peak.
Paper by default. Set OFTRAP_LIVE=1 only if you want S3 fan-out.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from app.services import cache_manager, telegram_notifier
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
MIN_RISK = float(os.environ.get("OFTRAP_MIN_OPT_RISK", "1.0"))


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
    status: str = "open"
    exit_px: float | None = None
    exit_reason: str = ""
    log: list[str] = field(default_factory=list)


class OfTrapOms:
    def __init__(self) -> None:
        self.paper = bool(MOCK_MODE) or os.environ.get("OFTRAP_LIVE", "0") not in ("1", "true", "True")
        self.client: UpstoxClient | None = None
        self.position: OfTrapPosition | None = None
        self.closed: list[dict[str, Any]] = []

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
    ) -> dict[str, Any]:
        """Resolve ITM option + same-TF candle → entry/SL/1:4. No order."""
        code = symbol.upper()
        cfg = INDEX_CONFIGS.get(code)
        if not cfg:
            raise ValueError(f"unsupported index {symbol}")
        if kind not in ("S", "B"):
            raise ValueError(f"trade only on S/B absorption, got {kind}")
        direction = "LONG" if kind == "S" else "SHORT"
        cli = client or self._client()
        picked = cli.get_itm_option_contract(cfg.spot_instrument_key, spot, direction)
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
            "symbol": code,
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
        }

    def on_absorption(self, symbol: str, kind: str, bar_time: str, spot: float) -> OfTrapPosition | None:
        if kind not in ("S", "B"):
            return None
        now = datetime.now(IST)
        if now.time() >= NO_ENTRY_AFTER:
            logger.info("skip %s %s — past %s", symbol, bar_time, NO_ENTRY_AFTER)
            return None
        if self.position and self.position.status == "open":
            logger.info("skip %s %s — already in %s %s", symbol, bar_time, self.position.kind, self.position.bar_time)
            return None
        try:
            plan = self.plan_from_absorption(symbol, kind, bar_time, spot)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OF Trap plan failed %s %s %s: %s", symbol, kind, bar_time, exc)
            return None
        pos = OfTrapPosition(
            symbol=plan["symbol"],
            kind=kind,
            direction=plan["direction"],
            option_side=plan["option_side"],
            bar_time=bar_time,
            instrument_key=plan["instrument_key"],
            strike=plan["strike"],
            expiry=plan["expiry"],
            entry=plan["entry"],
            sl=plan["sl"],
            target=plan["target"],
            r_pts=plan["r_pts"],
            option_ohlc=plan["option_ohlc"],
            opened_at=now.isoformat(),
            premium_high=plan["entry"],
            log=[
                f"{bar_time} {kind} → BUY {plan['option_side']} {plan['strike']} "
                f"entry {plan['entry']:.2f} SL {plan['sl']:.2f} TP {plan['target']:.2f} (1:{RR_TARGET:.0f})"
            ],
        )
        self.position = pos
        self._publish()
        logger.warning(
            "[%s %s] PAPER BUY %s%d  entry=%.2f SL=%.2f TP=%.2f R=%.2f",
            symbol, bar_time, pos.option_side, pos.strike, pos.entry, pos.sl, pos.target, pos.r_pts,
        )
        try:
            telegram_notifier.send_message(
                f"OF TRAP {kind} paper — {symbol} {bar_time}\n"
                f"BUY {pos.option_side} {pos.strike}\n"
                f"Opt {bar_time} C={pos.entry:.2f} L={pos.sl:.2f}\n"
                f"SL {pos.sl:.2f}  TP {pos.target:.2f} (1:{RR_TARGET:.0f})\n"
                f"Trail: 1R → cost, then +1/pt"
            )
        except Exception:  # noqa: BLE001
            logger.exception("telegram oftrap trade failed")
        return pos

    def poll(self) -> None:
        pos = self.position
        if pos is None or pos.status != "open":
            return
        now = datetime.now(IST)
        try:
            ltp = self._client().get_ltp(pos.instrument_key)
        except Exception:  # noqa: BLE001
            ltp = None
        if ltp is None:
            self._publish()
            return
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
        if ltp <= pos.sl:
            reason = "SL" if not pos.trail_armed else ("COST" if abs(pos.sl - pos.entry) < 0.05 else "TRAIL SL")
        elif ltp >= pos.target:
            reason = "TARGET 1:4"
        elif now.time() >= FORCE_EXIT:
            reason = "TIME EXIT 15:20"
        if reason:
            pos.status = "closed"
            pos.exit_px = ltp
            pos.exit_reason = reason
            pnl = round(ltp - pos.entry, 2)
            pos.log.append(f"exit {reason} @ {ltp:.2f}  Δ {pnl:+.2f}")
            self.closed.append(asdict(pos))
            logger.warning("[%s %s] EXIT %s @ %.2f (entry %.2f)", pos.symbol, pos.bar_time, reason, ltp, pos.entry)
            self.position = None
        self._publish()

    def _publish(self) -> None:
        pos = self.position
        payload = {
            "paper": self.paper,
            "updated": datetime.now(IST).isoformat(),
            "position": asdict(pos) if pos else None,
            "closed": self.closed[-8:],
        }
        try:
            cache_manager.set_json(cache_manager.OFTRAP_TRADE_KEY, payload, ttl_seconds=86_400)
        except Exception:  # noqa: BLE001
            pass
