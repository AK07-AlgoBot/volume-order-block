"""Orderflow Absorption-Trap engine (Upstox live footprint reconstruction).

NOT FINANCIAL ADVICE. Research / paper only — this engine places NO broker orders.
It reconstructs a 5-minute footprint from the Upstox ``full`` market-data feed and
runs the *exact same* absorption-trap logic as the GoCharting Lipi study
``scripts/gocharting/OFS_Absorption_Trap.lipi`` so the two can be compared live.

Why this is possible on Upstox
──────────────────────────────
GoCharting reads a classified tick tape (every trade tagged buy/sell) that Upstox
does not hand out. But the Upstox ``full`` / ``full_d30`` feed gives, per message:
  • ltp        last traded price
  • vtt        cumulative volume traded today  → per-message traded qty = Δvtt
  • bid/ask    top-of-book (marketLevel.bidAskQuote)
That is enough to classify aggressor by the Lee-Ready rule and bucket volume into
5-minute delta / per-price footprint. It is an approximation (trades between two
messages are lumped, and mid-spread prints use a tick test), but the decisive
gates in the trap logic are *relative* (buy/sell ratio, wick fraction, volume vs
its own SMA) and *price-point* based (location, distance moved), so the behaviour
tracks the Lipi closely regardless of absolute volume units.

What it emits (console + Telegram + Redis ``ak07:oftrap_state``)
  • S  = selling absorption at a low   (red circle in the Lipi)
  • B  = buying  absorption at a high  (green circle in the Lipi)
  • TRAP BUY / TRAP SELL = the actual signal (triangle in the Lipi)

Run:  python -u src/server/src/app/services/orderflow_trap_engine.py
Env:
  OFTRAP_SYMBOLS   comma list, default "NIFTY" (e.g. "NIFTY,BANKNIFTY")
  OFTRAP_IMB_RATIO 1.25   buy/sell volume dominance
  OFTRAP_WICK_MIN  0.28   min rejection wick fraction
  OFTRAP_VOL_MULT  0.9    min bar volume vs its SMA
  OFTRAP_VOL_SMA   20     volume SMA length (bars)
  OFTRAP_LOOKN     5      local-extreme lookback (bars)
  OFTRAP_LOC_TOL   35     near day/local H-L tolerance (Nifty pts; BN x4)
  OFTRAP_AWAY_PTS  40     min move before trap (Nifty pts; BN x2)
  OFTRAP_DFLOOR    180    |delta| for a "strong" bubble (Nifty; BN x2) — cosmetic
  OFTRAP_NEED_MISMATCH 1  require delta-vs-close mismatch for absorption
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services import cache_manager, telegram_notifier  # noqa: E402
from app.services.ofmap_bridge import build_feed_and_resolver  # noqa: E402
from app.services.orderflow_trap_oms import OfTrapOms  # noqa: E402

logger = logging.getLogger("ak07.oftrap")

IST: Final = ZoneInfo("Asia/Kolkata")
BAR_MS: Final[int] = 5 * 60 * 1000
SESSION_START: Final[dtime] = dtime(9, 15)
SESSION_END: Final[dtime] = dtime(15, 30)
TICK: Final[float] = 0.05


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class Params:
    imb_ratio: float = _f("OFTRAP_IMB_RATIO", 1.25)
    wick_min: float = _f("OFTRAP_WICK_MIN", 0.28)
    vol_mult: float = _f("OFTRAP_VOL_MULT", 0.9)
    vol_sma: int = _i("OFTRAP_VOL_SMA", 20)
    look_n: int = _i("OFTRAP_LOOKN", 5)
    loc_tol_pts: float = _f("OFTRAP_LOC_TOL", 35.0)
    away_pts: float = _f("OFTRAP_AWAY_PTS", 40.0)
    dfloor: float = _f("OFTRAP_DFLOOR", 180.0)
    need_mismatch: bool = os.environ.get("OFTRAP_NEED_MISMATCH", "1") not in ("0", "false", "False")


@dataclass
class Bar:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    buy_vol: float = 0.0
    sell_vol: float = 0.0
    volume: float = 0.0
    levels: dict[float, list[float]] = field(default_factory=dict)  # price -> [buy, sell]
    has_delta: bool = True   # False for REST-seeded warmup bars (no tape)
    sell_abs: bool = False
    buy_abs: bool = False

    @property
    def delta(self) -> float:
        return self.buy_vol - self.sell_vol


@dataclass
class SymState:
    symbol: str
    is_bn: bool
    bars: deque[Bar] = field(default_factory=lambda: deque(maxlen=80))
    current: Bar | None = None
    last_vtt: float | None = None
    last_ltp: float | None = None
    last_dir: str = "buy"
    day: str = ""
    last_closed_ms: int = 0


class OrderflowTrapEngine:
    def __init__(self, symbols: list[str]) -> None:
        self.params = Params()
        self.symbols = symbols
        self.states: dict[str, SymState] = {}
        self.key_to_symbol: dict[str, str] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=40)
        self._last_tick_mono: float = time.monotonic()
        self._published_day: str = ""
        self.oms = OfTrapOms()

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _bucket_ms(ts_ms: int) -> int:
        return (ts_ms // BAR_MS) * BAR_MS

    @staticmethod
    def _in_session(ts_ms: int) -> bool:
        t = datetime.fromtimestamp(ts_ms / 1000.0, tz=IST).time()
        return SESSION_START <= t <= SESSION_END

    def _classify(self, st: SymState, ltp: float, bid: float, ask: float) -> str:
        """Lee-Ready: at/above ask = buy, at/below bid = sell, else tick test."""
        if ask > 0 and ltp >= ask:
            return "buy"
        if bid > 0 and ltp <= bid:
            return "sell"
        if st.last_ltp is not None:
            if ltp > st.last_ltp:
                return "buy"
            if ltp < st.last_ltp:
                return "sell"
        return st.last_dir

    # ------------------------------------------------------------ bar plumbing
    def _reset_day(self, st: SymState, day: str) -> None:
        st.bars.clear()
        st.current = None
        st.last_vtt = None
        st.last_closed_ms = 0
        st.day = day

    def _open_bar(self, start_ms: int, price: float, *, has_delta: bool = True) -> Bar:
        return Bar(start_ms=start_ms, open=price, high=price, low=price, close=price, has_delta=has_delta)

    def on_payload(self, key: str, payload: dict[str, Any]) -> None:
        sym = self.key_to_symbol.get(key)
        if not sym:
            return
        st = self.states[sym]
        self._last_tick_mono = time.monotonic()
        try:
            ltp = float(payload.get("ltp"))
        except (TypeError, ValueError):
            return
        if ltp <= 0:
            return
        vtt = payload.get("volume")
        try:
            vtt = float(vtt) if vtt is not None else None
        except (TypeError, ValueError):
            vtt = None
        ltt = payload.get("ltt")
        try:
            ts_ms = int(ltt) if ltt is not None else int(time.time() * 1000)
        except (TypeError, ValueError):
            ts_ms = int(time.time() * 1000)

        depth = payload.get("depth") or {}
        bids = depth.get("buy") or []
        asks = depth.get("sell") or []
        bid = float(bids[0]["price"]) if bids else 0.0
        ask = float(asks[0]["price"]) if asks else 0.0

        # Day rollover / session gate.
        day = datetime.fromtimestamp(ts_ms / 1000.0, tz=IST).date().isoformat()
        if st.day != day:
            self._reset_day(st, day)
        if not self._in_session(ts_ms):
            st.last_ltp = ltp
            st.last_vtt = vtt if vtt is not None else st.last_vtt
            return

        # Per-message traded quantity from cumulative volume.
        vol = 0.0
        if vtt is not None:
            if st.last_vtt is not None and vtt >= st.last_vtt:
                vol = vtt - st.last_vtt
            st.last_vtt = vtt

        bucket = self._bucket_ms(ts_ms)
        if bucket <= st.last_closed_ms:
            # Late tick for a bar we already finalized (rollover timer beat it).
            st.last_ltp = ltp
            return
        if st.current is None:
            st.current = self._open_bar(bucket, ltp)
        elif bucket > st.current.start_ms:
            # Close the elapsed bar, then open the new one.
            self._finalize(st)
            st.current = self._open_bar(bucket, ltp)

        # Classify and accumulate.
        direction = self._classify(st, ltp, bid, ask)
        st.last_dir = direction
        st.last_ltp = ltp
        bar = st.current
        bar.high = max(bar.high, ltp)
        bar.low = min(bar.low, ltp)
        bar.close = ltp
        if vol > 0:
            bar.volume += vol
            price_key = round(ltp / TICK) * TICK
            lvl = bar.levels.setdefault(price_key, [0.0, 0.0])
            if direction == "buy":
                bar.buy_vol += vol
                lvl[0] += vol
            else:
                bar.sell_vol += vol
                lvl[1] += vol

    def tick_rollover(self) -> None:
        """Called on a timer so a bar still finalizes when trades go quiet near close."""
        now_ms = int(time.time() * 1000)
        cur_bucket = self._bucket_ms(now_ms)
        for st in self.states.values():
            if st.current is not None and cur_bucket > st.current.start_ms:
                if self._in_session(st.current.start_ms):
                    self._finalize(st)
                st.current = None

    # ------------------------------------------------------------- detection
    def _finalize(self, st: SymState) -> None:
        bar = st.current
        if bar is None:
            return
        self._detect(st, bar)
        st.bars.append(bar)
        st.last_closed_ms = bar.start_ms

    def _detect(self, st: SymState, bar: Bar) -> None:
        p = self.params
        loc_tol = p.loc_tol_pts * (4.0 if st.is_bn else 1.0)
        away = p.away_pts * (2.0 if st.is_bn else 1.0)
        dfloor = p.dfloor * (2.0 if st.is_bn else 1.0)

        rng = max(bar.high - bar.low, TICK)
        mid = (bar.high + bar.low) / 2.0
        lower_wick = (min(bar.open, bar.close) - bar.low) / rng
        upper_wick = (bar.high - max(bar.open, bar.close)) / rng

        recent = list(st.bars)  # closed bars before this one
        with_cur = recent + [bar]

        # Location over last look_n bars (incl. current), like talib.lowest/highest.
        look = with_cur[-p.look_n:]
        loc_lo = bar.low <= min(b.low for b in look) + loc_tol
        loc_hi = bar.high >= max(b.high for b in look) - loc_tol

        # Volume vs its SMA (relative → scale independent).
        vol_hist = with_cur[-p.vol_sma:]
        vol_sma = sum(b.volume for b in vol_hist) / max(len(vol_hist), 1)
        vol_ok = bar.volume >= vol_sma * p.vol_mult

        sell_dom = buy_dom = mismatch_up = mismatch_dn = False
        if bar.has_delta:
            sell_dom = bar.sell_vol >= bar.buy_vol * p.imb_ratio or bar.delta < 0.0
            buy_dom = bar.buy_vol >= bar.sell_vol * p.imb_ratio or bar.delta > 0.0
            mismatch_up = bar.delta < 0.0 and (bar.close > bar.open or bar.close >= mid)
            mismatch_dn = bar.delta > 0.0 and (bar.close < bar.open or bar.close <= mid)
            bar.sell_abs = (
                sell_dom and lower_wick >= p.wick_min and loc_lo and vol_ok
                and (not p.need_mismatch or mismatch_up)
            )
            bar.buy_abs = (
                buy_dom and upper_wick >= p.wick_min and loc_hi and vol_ok
                and (not p.need_mismatch or mismatch_dn)
            )
        skip = self._skip_reason(
            bar,
            p=p,
            lower_wick=lower_wick,
            upper_wick=upper_wick,
            loc_lo=loc_lo,
            loc_hi=loc_hi,
            vol_ok=vol_ok,
            sell_dom=sell_dom,
            buy_dom=buy_dom,
            mismatch_up=mismatch_up,
            mismatch_dn=mismatch_dn,
        )

        # Trap: opposite absorption after a move to the other extreme (last 12 bars).
        recent12 = recent[-12:]
        with12 = with_cur[-12:]
        sess_hi = max(b.high for b in with12)
        sess_lo = min(b.low for b in with12)
        near_hi = bar.high >= sess_hi - loc_tol
        near_lo = bar.low <= sess_lo + loc_tol
        moved_up = bar.close - sess_lo >= away
        moved_dn = sess_hi - bar.close >= away
        seen_sell_abs = any(b.sell_abs for b in recent12)
        seen_buy_abs = any(b.buy_abs for b in recent12)

        trap_short = seen_sell_abs and bar.buy_abs and near_hi and moved_up   # SELL
        trap_long = seen_buy_abs and bar.sell_abs and near_lo and moved_dn    # BUY
        if trap_short and trap_long:
            trap_short = trap_long = False

        strong = abs(bar.delta) >= dfloor or (vol_sma > 0 and bar.volume / vol_sma >= 1.4)
        self._log_bar(st, bar, strong=strong, skip=skip)
        if bar.sell_abs:
            self._emit(st, bar, "S", "Selling absorption at low")
        if bar.buy_abs:
            self._emit(st, bar, "B", "Buying absorption at high")
        if trap_long:
            self._emit(st, bar, "TRAP_BUY", "Buying absorbed earlier → drop → selling now absorbed at low")
        if trap_short:
            self._emit(st, bar, "TRAP_SELL", "Selling absorbed earlier → rally → buying now absorbed at high")

        self._publish(st, bar, trap_long=trap_long, trap_short=trap_short, skip=skip)

    # -------------------------------------------------------------- reporting
    @staticmethod
    def _bar_time(bar: Bar) -> str:
        return datetime.fromtimestamp(bar.start_ms / 1000.0, tz=IST).strftime("%H:%M")

    @staticmethod
    def _skip_reason(
        bar: Bar,
        *,
        p: Params,
        lower_wick: float,
        upper_wick: float,
        loc_lo: bool,
        loc_hi: bool,
        vol_ok: bool,
        sell_dom: bool,
        buy_dom: bool,
        mismatch_up: bool,
        mismatch_dn: bool,
    ) -> str:
        if bar.sell_abs or bar.buy_abs:
            return ""
        if not bar.has_delta:
            return "no tape (warmup)"
        bits: list[str] = []
        if sell_dom:
            if lower_wick < p.wick_min:
                bits.append(f"S wick {lower_wick:.2f}<{p.wick_min:.2f}")
            if not loc_lo:
                bits.append("S not local low")
            if not vol_ok:
                bits.append("S vol thin")
            if p.need_mismatch and not mismatch_up:
                bits.append("S close at low (need recovery)")
        elif buy_dom:
            if upper_wick < p.wick_min:
                bits.append(f"B wick {upper_wick:.2f}<{p.wick_min:.2f}")
            if not loc_hi:
                bits.append("B not local high")
            if not vol_ok:
                bits.append("B vol thin")
            if p.need_mismatch and not mismatch_dn:
                bits.append("B close at high (need rejection)")
        else:
            bits.append("no buy/sell dominance")
        return "; ".join(bits)

    def _log_bar(self, st: SymState, bar: Bar, *, strong: bool, skip: str = "") -> None:
        tag = "S" if bar.sell_abs else ("B" if bar.buy_abs else "·")
        extra = f"  skip={skip}" if skip else ""
        logger.info(
            "[%s %s] O%.1f H%.1f L%.1f C%.1f  buy=%.0f sell=%.0f Δ=%+.0f vol=%.0f  %s%s%s",
            st.symbol, self._bar_time(bar), bar.open, bar.high, bar.low, bar.close,
            bar.buy_vol, bar.sell_vol, bar.delta, bar.volume, tag, " *" if strong else "", extra,
        )

    def _emit(self, st: SymState, bar: Bar, kind: str, detail: str) -> None:
        t = self._bar_time(bar)
        self.events.append({
            "symbol": st.symbol,
            "time": t,
            "kind": kind,
            "price": round(bar.close, 2),
            "delta": round(bar.delta),
            "detail": detail,
        })
        self._publish_events()
        if kind in ("TRAP_BUY", "TRAP_SELL"):
            side = "BUY" if kind == "TRAP_BUY" else "SELL"
            logger.warning("[%s %s] *** TRAP %s *** %s | C=%.2f Δ=%+.0f", st.symbol, t, side, detail, bar.close, bar.delta)
            try:
                telegram_notifier.send_message(
                    f"\U0001f9f2 *OF TRAP {side}* — {st.symbol}\n"
                    f"\u2022 Time: {t} (5m close)\n"
                    f"\u2022 Price: {bar.close:.2f}  Δ: {bar.delta:+.0f}\n"
                    f"\u2022 {detail}\n"
                    f"\u2022 Paper/compare-vs-GoCharting only"
                )
            except Exception:  # noqa: BLE001 - notifier must never kill the feed
                logger.exception("telegram emit failed")
        else:
            logger.info("[%s %s] %s — %s (C=%.2f Δ=%+.0f)", st.symbol, t, kind, detail, bar.close, bar.delta)
        if kind in ("S", "B"):
            try:
                self.oms.on_absorption(st.symbol, kind, t, float(bar.close))
            except Exception:  # noqa: BLE001
                logger.exception("OF Trap OMS entry failed")

    def _clear_stale_session(self, today: str) -> None:
        """Drop yesterday's Redis snapshot so the dashboard cannot look live."""
        self.events.clear()
        self._publish_events()
        for st in self.states.values():
            self._reset_day(st, today)
            try:
                cache_manager.set_json(
                    cache_manager.OFTRAP_STATE_KEY_TEMPLATE.format(symbol=st.symbol),
                    {
                        "symbol": st.symbol,
                        "time": "—",
                        "open": None, "high": None, "low": None, "close": None,
                        "buy_vol": 0, "sell_vol": 0, "delta": 0, "volume": 0,
                        "sell_abs": False, "buy_abs": False,
                        "trap_buy": False, "trap_sell": False,
                        "skip": "",
                        "updated": datetime.now(IST).isoformat(),
                        "status": "waiting",
                    },
                    ttl_seconds=86_400,
                )
            except Exception:  # noqa: BLE001
                pass
        self._published_day = today
        logger.info("cleared stale OF Trap session — waiting for %s tape", today)

    def _publish_events(self) -> None:
        try:
            cache_manager.set_json(
                cache_manager.OFTRAP_EVENTS_KEY, list(self.events), ttl_seconds=86_400
            )
        except Exception:  # noqa: BLE001
            pass

    def _publish(self, st: SymState, bar: Bar, *, trap_long: bool, trap_short: bool, skip: str = "") -> None:
        try:
            cache_manager.set_json(
                cache_manager.OFTRAP_STATE_KEY_TEMPLATE.format(symbol=st.symbol),
                {
                    "symbol": st.symbol,
                    "time": self._bar_time(bar),
                    "open": round(bar.open, 2),
                    "high": round(bar.high, 2),
                    "low": round(bar.low, 2),
                    "close": round(bar.close, 2),
                    "buy_vol": round(bar.buy_vol),
                    "sell_vol": round(bar.sell_vol),
                    "delta": round(bar.delta),
                    "volume": round(bar.volume),
                    "sell_abs": bar.sell_abs,
                    "buy_abs": bar.buy_abs,
                    "trap_buy": trap_long,
                    "trap_sell": trap_short,
                    "skip": skip,
                    "updated": datetime.now(IST).isoformat(),
                    "status": "live",
                },
                ttl_seconds=86_400,
            )
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------------- warmup
    def _seed_warmup(self, resolver_keys: dict[str, str]) -> None:
        """Seed today's closed 5m OHLCV (price-only, no delta) for location/SMA context."""
        try:
            from app.services.upstox_engine import build_upstox_client
            client = build_upstox_client()
        except Exception as exc:  # noqa: BLE001
            logger.info("warmup seeding skipped (no client): %s", exc)
            return
        for sym, key in resolver_keys.items():
            st = self.states[sym]
            try:
                candles = client.get_closed_5min_candles(key) or []
            except Exception as exc:  # noqa: BLE001
                logger.info("[%s] warmup fetch failed: %s", sym, exc)
                continue
            seeded = 0
            for c in candles:
                try:
                    o = float(c["open"]); h = float(c["high"]); l = float(c["low"]); cl = float(c["close"])
                    v = float(c.get("volume") or 0.0)
                    ts = c.get("timestamp") or c.get("time") or c.get("ts")
                    ts_ms = int(datetime.fromisoformat(str(ts)).timestamp() * 1000) if ts else 0
                except (KeyError, TypeError, ValueError):
                    continue
                if ts_ms and not self._in_session(ts_ms):
                    continue
                bar = Bar(start_ms=self._bucket_ms(ts_ms), open=o, high=h, low=l, close=cl, volume=v, has_delta=False)
                st.bars.append(bar)
                seeded += 1
            if seeded:
                st.day = datetime.now(IST).date().isoformat()
                logger.info("[%s] seeded %d warmup bars (price-only, no delta)", sym, seeded)

    # -------------------------------------------------------------------- run
    async def run(self) -> None:
        feed, resolver = build_feed_and_resolver()
        keys: dict[str, str] = {}
        for sym in self.symbols:
            key = resolver.resolve(sym, "NFO")
            if not key:
                logger.error("could not resolve %s future — skipping", sym)
                continue
            keys[sym] = key
            self.key_to_symbol[key] = sym
            self.states[sym] = SymState(symbol=sym, is_bn=("BANK" in sym.upper()))
            logger.info("subscribing %s → %s", sym, key)
        if not keys:
            logger.error("no instruments resolved; exiting")
            return

        self._seed_warmup(keys)
        today = datetime.now(IST).date().isoformat()
        for st in self.states.values():
            if st.bars:
                self._publish(st, st.bars[-1], trap_long=False, trap_short=False)
                self._published_day = today
            else:
                self._clear_stale_session(today)
                break

        queues: dict[str, asyncio.Queue] = {}
        for key in keys.values():
            q: asyncio.Queue = asyncio.Queue(maxsize=1000)
            feed.register_queue(key, q)
            queues[key] = q
        feed.start()

        async def consume(key: str, q: asyncio.Queue) -> None:
            while True:
                payload = await q.get()
                try:
                    self.on_payload(key, payload)
                except Exception:  # noqa: BLE001 - one bad tick must not kill the stream
                    logger.exception("[%s] payload error", self.key_to_symbol.get(key, key))

        async def roller() -> None:
            while True:
                await asyncio.sleep(3)
                try:
                    self.tick_rollover()
                    self.oms.poll()
                except Exception:  # noqa: BLE001
                    logger.exception("rollover error")

        async def watchdog() -> None:
            """Clock day-reset + exit if the Upstox feed is silent in session (docker restarts)."""
            grace = time.monotonic() + 180
            while True:
                await asyncio.sleep(20)
                now = datetime.now(IST)
                today = now.date().isoformat()
                in_session = SESSION_START <= now.time() <= SESSION_END
                if in_session and self._published_day != today:
                    self._clear_stale_session(today)
                silent = time.monotonic() - self._last_tick_mono
                if in_session and time.monotonic() > grace and silent > 120:
                    logger.error(
                        "no live ticks for %.0fs in session — exiting so docker restarts the feed",
                        silent,
                    )
                    os._exit(1)

        logger.info(
            "OF Trap engine live | oms=%s | symbols=%s | imb=%.2f wick=%.2f volx=%.2f lookN=%d loc=%.0f away=%.0f",
            "PAPER" if self.oms.paper else "LIVE",
            ",".join(keys), self.params.imb_ratio, self.params.wick_min, self.params.vol_mult,
            self.params.look_n, self.params.loc_tol_pts, self.params.away_pts,
        )
        tasks = [asyncio.create_task(consume(k, q)) for k, q in queues.items()]
        tasks.append(asyncio.create_task(roller()))
        tasks.append(asyncio.create_task(watchdog()))
        await asyncio.gather(*tasks)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    symbols = [s.strip().upper() for s in os.environ.get("OFTRAP_SYMBOLS", "NIFTY").split(",") if s.strip()]
    engine = OrderflowTrapEngine(symbols)
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        logger.info("stopped")


if __name__ == "__main__":
    main()
