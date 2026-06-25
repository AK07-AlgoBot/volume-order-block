"""Gamma blast / hero-zero analytics — chain read + spot backtest proxy.

Live: uses Upstox option chain (pin strike, GEX, blast score).
Backtest: spot-only proxy on historical expiry days (no chain archive).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.services.options_greeks import (
    ChainAnalytics,
    analyze_option_chain,
    bs_price,
    years_to_expiry,
)
from app.services.upstox_engine import IndexConfig

IST: Final = ZoneInfo("Asia/Kolkata")

# Defaults — overridden by backtest refinement / env
DEFAULT_PIN_DISTANCE_PCT: Final[float] = 0.20
DEFAULT_MIN_IDR_PCT: Final[float] = 0.55
DEFAULT_OTM_STRIKES: Final[int] = 2
DEFAULT_HERO_TP_MULT: Final[float] = 2.0
DEFAULT_HERO_SL_MULT: Final[float] = 0.50
DEFAULT_MIN_BLAST_SCORE: Final[int] = 55
DEFAULT_IV_ASSUMPTION: Final[float] = 0.22

BLAST_WINDOW_START: Final[dtime] = dtime(13, 30)
BLAST_WINDOW_END: Final[dtime] = dtime(15, 0)
SESSION_END: Final[dtime] = dtime(15, 30)


@dataclass
class GammaConfig:
    pin_distance_pct: float = DEFAULT_PIN_DISTANCE_PCT
    min_idr_pct: float = DEFAULT_MIN_IDR_PCT
    otm_strikes: int = DEFAULT_OTM_STRIKES
    hero_tp_mult: float = DEFAULT_HERO_TP_MULT
    hero_sl_mult: float = DEFAULT_HERO_SL_MULT
    min_blast_score: int = DEFAULT_MIN_BLAST_SCORE
    iv_assumption: float = DEFAULT_IV_ASSUMPTION


@dataclass
class GammaSnapshot:
    index_code: str
    is_expiry_day: bool
    expiry_date: str
    expiry_rule: str
    spot: float
    pin_strike: int
    call_wall: int
    put_floor: int
    pin_distance_pts: float
    pin_distance_pct: float
    idr_pct: float
    blast_score: int
    regime: str
    bias: str
    gamma_flip: float | None
    pcr_oi: float
    atm_iv: float
    blast_window_active: bool
    observer_signal: str
    observer_detail: str
    paper_hero: dict[str, Any] | None = None
    signal_log: list[str] = field(default_factory=list)


@dataclass
class HeroBacktestTrade:
    index_code: str
    day: date
    direction: str
    option_type: str
    strike: int
    entry_premium: float
    exit_premium: float
    pnl_premium: float
    entry_at: str
    exit_at: str
    exit_reason: str
    blast_score: int


def round_strike(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


def pin_strike_from_chain(chain_rows: list[dict[str, Any]], spot: float, band: float = 800.0) -> tuple[int, int, int]:
    """Return (pin, call_wall, put_floor) from chain OI."""
    best_pin: tuple[float, int] | None = None
    best_call: tuple[float, int] | None = None
    best_put: tuple[float, int] | None = None
    for row in chain_rows:
        try:
            strike = int(float(row.get("strike_price", 0)))
        except (TypeError, ValueError):
            continue
        if abs(strike - spot) > band:
            continue
        call_oi = float(((row.get("call_options") or {}).get("market_data") or {}).get("oi") or 0)
        put_oi = float(((row.get("put_options") or {}).get("market_data") or {}).get("oi") or 0)
        combined = call_oi + put_oi
        if best_pin is None or combined > best_pin[0]:
            best_pin = (combined, strike)
        if best_call is None or call_oi > best_call[0]:
            best_call = (call_oi, strike)
        if best_put is None or put_oi > best_put[0]:
            best_put = (put_oi, strike)
    pin = best_pin[1] if best_pin else round_strike(spot, 50)
    call_wall = best_call[1] if best_call else pin
    put_floor = best_put[1] if best_put else pin
    return pin, call_wall, put_floor


def compute_blast_score(
    *,
    pin_distance_pct: float,
    idr_pct: float,
    analytics: ChainAnalytics | None,
    blast_window_active: bool,
    cfg: GammaConfig,
) -> int:
    score = 0.0
    if pin_distance_pct <= cfg.pin_distance_pct:
        score += 35.0 * (1.0 - pin_distance_pct / max(cfg.pin_distance_pct, 1e-6))
    if idr_pct >= cfg.min_idr_pct:
        score += min(25.0, 25.0 * (idr_pct / cfg.min_idr_pct))
    if analytics:
        if analytics.regime == "NEGATIVE_GEX":
            score += 20.0
        if analytics.gamma_flip is not None and abs(analytics.spot - analytics.gamma_flip) <= analytics.spot * 0.003:
            score += 10.0
        if analytics.pcr_oi >= 1.05 or analytics.pcr_oi <= 0.95:
            score += 10.0
    if blast_window_active:
        score += 10.0
    return int(max(0.0, min(100.0, score)))


def observer_signal_from_snapshot(
    *,
    spot: float,
    pin: int,
    pin_distance_pct: float,
    idr_pct: float,
    blast_score: int,
    analytics: ChainAnalytics | None,
    blast_window_active: bool,
    cfg: GammaConfig,
    prev_spot: float | None = None,
) -> tuple[str, str, dict[str, Any] | None]:
    """Paper-only hero observer — no broker orders."""
    if not blast_window_active:
        if pin_distance_pct <= cfg.pin_distance_pct * 1.5:
            return "PIN_WATCH", f"Spot {spot:.0f} near pin {pin} ({pin_distance_pct:.2f}%) — blast window 13:30", None
        return "WAIT", "Pre-blast window — monitoring pin + range", None

    if blast_score < cfg.min_blast_score:
        return "LOW_SCORE", f"Blast score {blast_score} < {cfg.min_blast_score} — wait", None

    if idr_pct < cfg.min_idr_pct:
        return "LOW_RANGE", f"IDR {idr_pct:.2f}% below {cfg.min_idr_pct}% — skip hero", None

    momentum_up = prev_spot is not None and spot > prev_spot + 0.0002 * spot
    momentum_dn = prev_spot is not None and spot < prev_spot - 0.0002 * spot

    if spot > pin and momentum_up:
        detail = f"Spot broke above pin {pin} · score {blast_score} · NEG_GEX={analytics.regime if analytics else 'n/a'}"
        hero = {"side": "CE", "direction": "LONG", "reason": detail}
        return "HERO_PAPER_CE", detail, hero
    if spot < pin and momentum_dn:
        detail = f"Spot broke below pin {pin} · score {blast_score} · NEG_GEX={analytics.regime if analytics else 'n/a'}"
        hero = {"side": "PE", "direction": "SHORT", "reason": detail}
        return "HERO_PAPER_PE", detail, hero

    return "BLAST_WATCH", f"Pin {pin} · score {blast_score} · need directional break", None


def _ltp_from_chain(chain_rows: list[dict[str, Any]], strike: int, option_type: str) -> float | None:
    for row in chain_rows:
        try:
            row_strike = int(float(row.get("strike_price", 0)))
        except (TypeError, ValueError):
            continue
        if row_strike != strike:
            continue
        leg = row.get("call_options") if option_type == "CE" else row.get("put_options")
        ltp = ((leg or {}).get("market_data") or {}).get("ltp")
        if ltp is not None:
            return float(ltp)
    return None


def build_paper_hero_plan(
    *,
    cfg: IndexConfig,
    spot: float,
    pin: int,
    hero: dict[str, Any],
    chain_rows: list[dict[str, Any]],
    expiry_date: str,
    now: datetime,
    params: GammaConfig,
    blast_score: int,
    regime: str,
    signal: str,
    detail: str,
) -> dict[str, Any]:
    """Enrich paper hero with strike, chain/BS premium, and TP/SL targets for logs."""
    side = str(hero.get("side") or "CE")
    otm_offset = params.otm_strikes * cfg.strike_step
    strike = pin + otm_offset if side == "CE" else pin - otm_offset

    entry_premium = _ltp_from_chain(chain_rows, strike, side)
    premium_source = "chain_ltp"
    if entry_premium is None or entry_premium <= 0:
        t_years = years_to_expiry(expiry_date, now)
        entry_premium = bs_price(spot, strike, t_years, params.iv_assumption, option_type=side)
        premium_source = "bs_model"

    entry_premium = round(float(entry_premium), 2)
    tp_premium = round(entry_premium * params.hero_tp_mult, 2)
    sl_premium = round(entry_premium * params.hero_sl_mult, 2)

    return {
        **hero,
        "signal": signal,
        "index": cfg.code,
        "strike": strike,
        "option_type": side,
        "entry_premium": entry_premium,
        "tp_premium": tp_premium,
        "sl_premium": sl_premium,
        "tp_mult": params.hero_tp_mult,
        "sl_mult": params.hero_sl_mult,
        "premium_source": premium_source,
        "spot_at_signal": round(spot, 2),
        "pin_strike": pin,
        "blast_score": blast_score,
        "regime": regime,
        "expiry_date": expiry_date,
        "detail": detail,
        "otm_strikes": params.otm_strikes,
    }


def build_live_snapshot(
    *,
    cfg: IndexConfig,
    now: datetime,
    spot: float,
    day_high: float,
    day_low: float,
    day_open: float,
    expiry_date: str,
    expiry_rule: str,
    chain_rows: list[dict[str, Any]],
    prev_spot: float | None,
    cfg_params: GammaConfig,
    signal_log: list[str],
) -> GammaSnapshot:
    pin, call_wall, put_floor = pin_strike_from_chain(chain_rows, spot)
    pin_dist_pts = abs(spot - pin)
    pin_dist_pct = 100.0 * pin_dist_pts / spot if spot else 0.0
    idr_pct = 100.0 * (day_high - day_low) / day_open if day_open else 0.0

    analytics = analyze_option_chain(spot=spot, chain_rows=chain_rows, expiry=expiry_date, now=now)
    t = now.time()
    blast_active = BLAST_WINDOW_START <= t <= BLAST_WINDOW_END

    score = compute_blast_score(
        pin_distance_pct=pin_dist_pct,
        idr_pct=idr_pct,
        analytics=analytics,
        blast_window_active=blast_active,
        cfg=cfg_params,
    )
    signal, detail, hero = observer_signal_from_snapshot(
        spot=spot,
        pin=pin,
        pin_distance_pct=pin_dist_pct,
        idr_pct=idr_pct,
        blast_score=score,
        analytics=analytics,
        blast_window_active=blast_active,
        cfg=cfg_params,
        prev_spot=prev_spot,
    )

    if hero is not None:
        hero = build_paper_hero_plan(
            cfg=cfg,
            spot=spot,
            pin=pin,
            hero=hero,
            chain_rows=chain_rows,
            expiry_date=expiry_date,
            now=now,
            params=cfg_params,
            blast_score=score,
            regime=analytics.regime if analytics else "UNKNOWN",
            signal=signal,
            detail=detail,
        )

    return GammaSnapshot(
        index_code=cfg.code,
        is_expiry_day=True,
        expiry_date=expiry_date,
        expiry_rule=expiry_rule,
        spot=spot,
        pin_strike=pin,
        call_wall=call_wall,
        put_floor=put_floor,
        pin_distance_pts=round(pin_dist_pts, 2),
        pin_distance_pct=round(pin_dist_pct, 3),
        idr_pct=round(idr_pct, 3),
        blast_score=score,
        regime=analytics.regime if analytics else "UNKNOWN",
        bias=analytics.bias if analytics else "NEUTRAL",
        gamma_flip=analytics.gamma_flip if analytics else None,
        pcr_oi=analytics.pcr_oi if analytics else 0.0,
        atm_iv=analytics.atm_iv if analytics else 0.0,
        blast_window_active=blast_active,
        observer_signal=signal,
        observer_detail=detail,
        paper_hero=hero,
        signal_log=signal_log[-20:],
    )


def _session_candles_for_day(candles: list[dict[str, float]], day: date) -> list[dict[str, float]]:
    out = []
    for c in candles:
        ts = datetime.fromisoformat(str(c["timestamp"]))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        if ts.date() == day and ts.time() >= dtime(9, 15):
            out.append(c)
    return out


def _bar_close_ts(candle: dict[str, float], minutes: int = 5) -> datetime:
    ts = datetime.fromisoformat(str(candle["timestamp"]))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    return ts + timedelta(minutes=minutes)


def simulate_hero_on_expiry_day(
    *,
    cfg: IndexConfig,
    day: date,
    session_candles: list[dict[str, float]],
    params: GammaConfig,
) -> HeroBacktestTrade | None:
    """Spot-only hero proxy: enter at 13:30 if pin+range rules pass; walk to 15:00."""
    bars = _session_candles_for_day(session_candles, day)
    if len(bars) < 20:
        return None

    day_open = float(bars[0]["open"])
    day_high = max(float(b["high"]) for b in bars)
    day_low = min(float(b["low"]) for b in bars)

    entry_bar = None
    entry_idx = -1
    for i, bar in enumerate(bars):
        close_ts = _bar_close_ts(bar)
        if close_ts.time() == BLAST_WINDOW_START:
            entry_bar = bar
            entry_idx = i
            break
    if entry_bar is None:
        for i, bar in enumerate(bars):
            close_ts = _bar_close_ts(bar)
            if close_ts.time() >= BLAST_WINDOW_START:
                entry_bar = bar
                entry_idx = i
                break
    if entry_bar is None or entry_idx < 0:
        return None

    spot = float(entry_bar["close"])
    pin = round_strike(spot, cfg.strike_step)
    pin_dist_pct = 100.0 * abs(spot - pin) / spot if spot else 999.0
    idr_pct = 100.0 * (day_high - day_low) / day_open if day_open else 0.0

    score = compute_blast_score(
        pin_distance_pct=pin_dist_pct,
        idr_pct=idr_pct,
        analytics=None,
        blast_window_active=True,
        cfg=params,
    )
    if score < params.min_blast_score or pin_dist_pct > params.pin_distance_pct or idr_pct < params.min_idr_pct:
        return None

    prev_close = float(bars[entry_idx - 1]["close"]) if entry_idx > 0 else spot
    if spot > pin and spot > prev_close:
        opt = "CE"
        direction = "LONG"
        strike = pin + params.otm_strikes * cfg.strike_step
    elif spot < pin and spot < prev_close:
        opt = "PE"
        direction = "SHORT"
        strike = pin - params.otm_strikes * cfg.strike_step
    else:
        return None

    entry_at = _bar_close_ts(entry_bar)
    t_years = years_to_expiry(day.isoformat(), entry_at)
    entry_premium = bs_price(spot, strike, t_years, params.iv_assumption, option_type=opt)
    if entry_premium < 0.5:
        return None

    tp = entry_premium * params.hero_tp_mult
    sl = entry_premium * params.hero_sl_mult
    exit_premium = entry_premium
    exit_at = entry_at
    exit_reason = "SESSION_END"

    for bar in bars[entry_idx + 1 :]:
        close_ts = _bar_close_ts(bar)
        if close_ts.time() > SESSION_END:
            break
        bar_spot = float(bar["close"])
        t_years = years_to_expiry(day.isoformat(), close_ts)
        prem = bs_price(bar_spot, strike, t_years, params.iv_assumption, option_type=opt)
        if prem >= tp:
            exit_premium = tp
            exit_at = close_ts
            exit_reason = f"HERO_TP_{params.hero_tp_mult}x"
            break
        if prem <= sl:
            exit_premium = sl
            exit_at = close_ts
            exit_reason = "HERO_SL"
            break
        exit_premium = prem
        exit_at = close_ts

    return HeroBacktestTrade(
        index_code=cfg.code,
        day=day,
        direction=direction,
        option_type=opt,
        strike=strike,
        entry_premium=round(entry_premium, 2),
        exit_premium=round(exit_premium, 2),
        pnl_premium=round(exit_premium - entry_premium, 2),
        entry_at=entry_at.isoformat(),
        exit_at=exit_at.isoformat(),
        exit_reason=exit_reason,
        blast_score=score,
    )


def refine_gamma_config(
    trades_by_params: dict[tuple, list[HeroBacktestTrade]],
) -> GammaConfig:
    """Pick params with best premium expectancy (min 30 trades)."""
    best_cfg: GammaConfig | None = None
    best_expect = -999.0
    for key, trades in trades_by_params.items():
        if len(trades) < 30:
            continue
        expect = sum(t.pnl_premium for t in trades) / len(trades)
        if expect > best_expect:
            best_expect = expect
            pin_pct, idr_pct, otm, tp_mult = key
            best_cfg = GammaConfig(
                pin_distance_pct=pin_pct,
                min_idr_pct=idr_pct,
                otm_strikes=otm,
                hero_tp_mult=tp_mult,
            )
    return best_cfg or GammaConfig()


def grid_search_configs() -> list[GammaConfig]:
    configs: list[GammaConfig] = []
    for pin in (0.15, 0.20, 0.25, 0.30):
        for idr in (0.45, 0.55, 0.65):
            for otm in (1, 2, 3):
                for tp in (1.5, 2.0, 2.5):
                    configs.append(
                        GammaConfig(
                            pin_distance_pct=pin,
                            min_idr_pct=idr,
                            otm_strikes=otm,
                            hero_tp_mult=tp,
                        )
                    )
    return configs
