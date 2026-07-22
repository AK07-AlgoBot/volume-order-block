"""Black-Scholes greeks + option-chain analytics for index intraday strategies.

Computes IV from market LTP, delta/gamma exposure proxies, skew, and a
gamma-flip level from OI-weighted chain data (dealer-style positioning read).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

IST: Final = ZoneInfo("Asia/Kolkata")
RISK_FREE: Final[float] = 0.065
MIN_IV: Final[float] = 0.05
MAX_IV: Final[float] = 1.50


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(
    spot: float,
    strike: float,
    t_years: float,
    sigma: float,
    *,
    option_type: str,
    rate: float = RISK_FREE,
) -> float:
    if spot <= 0 or strike <= 0:
        return 0.0
    if t_years <= 1e-6 or sigma <= 1e-6:
        intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
        return intrinsic
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / (sigma * math.sqrt(t_years))
    d2 = d1 - sigma * math.sqrt(t_years)
    if option_type == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * t_years) * _norm_cdf(d2)
    return strike * math.exp(-rate * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def bs_delta(
    spot: float,
    strike: float,
    t_years: float,
    sigma: float,
    *,
    option_type: str,
    rate: float = RISK_FREE,
) -> float:
    if t_years <= 1e-6 or sigma <= 1e-6 or spot <= 0 or strike <= 0:
        if option_type == "CE":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / (sigma * math.sqrt(t_years))
    return _norm_cdf(d1) if option_type == "CE" else _norm_cdf(d1) - 1.0


def bs_gamma(spot: float, strike: float, t_years: float, sigma: float, *, rate: float = RISK_FREE) -> float:
    if t_years <= 1e-6 or sigma <= 1e-6 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / (sigma * math.sqrt(t_years))
    return _norm_pdf(d1) / (spot * sigma * math.sqrt(t_years))


def implied_volatility(
    market_price: float,
    spot: float,
    strike: float,
    t_years: float,
    *,
    option_type: str,
) -> float | None:
    if market_price <= 0.05 or spot <= 0 or strike <= 0 or t_years <= 1e-6:
        return None
    intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
    if market_price <= intrinsic + 0.05:
        return MIN_IV
    sigma = 0.25
    for _ in range(40):
        price = bs_price(spot, strike, t_years, sigma, option_type=option_type)
        vega = spot * _norm_pdf(
            (math.log(spot / strike) + (RISK_FREE + 0.5 * sigma * sigma) * t_years)
            / (sigma * math.sqrt(t_years))
        ) * math.sqrt(t_years)
        diff = price - market_price
        if abs(diff) < 0.01:
            return max(MIN_IV, min(MAX_IV, sigma))
        if abs(vega) < 1e-8:
            break
        sigma = max(MIN_IV, min(MAX_IV, sigma - diff / vega))
    return max(MIN_IV, min(MAX_IV, sigma))


def years_to_expiry(expiry: str, now: datetime | None = None) -> float:
    now = now or datetime.now(IST)
    try:
        exp = date.fromisoformat(str(expiry)[:10])
    except ValueError:
        return 1.0 / 365.0
    # Index weekly expiry — treat session close as expiry anchor
    exp_dt = datetime.combine(exp, datetime.strptime("15:30", "%H:%M").time(), tzinfo=IST)
    seconds = max((exp_dt - now).total_seconds(), 3600.0)
    return seconds / (365.0 * 24.0 * 3600.0)


@dataclass
class ChainAnalytics:
    spot: float
    expiry: str
    atm_strike: int
    atm_iv: float
    skew_pct: float
    pcr_oi: float
    net_delta_oi: float
    net_gex: float
    gamma_flip: float | None
    regime: str
    bias: str


def _ltp_from_leg(leg: dict[str, Any] | None) -> float:
    if not leg:
        return 0.0
    md = leg.get("market_data") or {}
    for key in ("ltp", "last_price", "close"):
        val = md.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return 0.0


def _oi_from_leg(leg: dict[str, Any] | None) -> float:
    if not leg:
        return 0.0
    md = leg.get("market_data") or {}
    try:
        return float(md.get("oi") or 0)
    except (TypeError, ValueError):
        return 0.0


def analyze_option_chain(
    *,
    spot: float,
    chain_rows: list[dict[str, Any]],
    expiry: str,
    band_points: float = 600.0,
    now: datetime | None = None,
) -> ChainAnalytics | None:
    """Build dealer-style chain read from Upstox option/chain rows."""
    if spot <= 0 or not chain_rows:
        return None

    t = years_to_expiry(expiry, now)
    strikes: list[dict[str, Any]] = []
    for row in chain_rows:
        try:
            strike = int(float(row.get("strike_price", 0)))
        except (TypeError, ValueError):
            continue
        if abs(strike - spot) > band_points:
            continue
        call = row.get("call_options") or {}
        put = row.get("put_options") or {}
        call_ltp = _ltp_from_leg(call)
        put_ltp = _ltp_from_leg(put)
        call_oi = _oi_from_leg(call)
        put_oi = _oi_from_leg(put)
        call_iv = implied_volatility(call_ltp, spot, strike, t, option_type="CE") if call_ltp > 0 else None
        put_iv = implied_volatility(put_ltp, spot, strike, t, option_type="PE") if put_ltp > 0 else None
        iv = call_iv or put_iv or 0.18
        call_delta = bs_delta(spot, strike, t, iv, option_type="CE")
        put_delta = bs_delta(spot, strike, t, iv, option_type="PE")
        gamma = bs_gamma(spot, strike, t, iv)
        strikes.append(
            {
                "strike": strike,
                "call_oi": call_oi,
                "put_oi": put_oi,
                "call_iv": call_iv or iv,
                "put_iv": put_iv or iv,
                "call_delta": call_delta,
                "put_delta": put_delta,
                "gamma": gamma,
            }
        )

    if not strikes:
        return None

    strikes.sort(key=lambda r: r["strike"])
    atm = min(strikes, key=lambda r: abs(r["strike"] - spot))
    atm_strike = int(atm["strike"])
    atm_iv = float((atm["call_iv"] + atm["put_iv"]) / 2.0)

    otm_puts = [s for s in strikes if s["strike"] < spot - 50]
    otm_calls = [s for s in strikes if s["strike"] > spot + 50]
    put_iv_ref = float(otm_puts[len(otm_puts) // 2]["put_iv"]) if otm_puts else atm["put_iv"]
    call_iv_ref = float(otm_calls[len(otm_calls) // 2]["call_iv"]) if otm_calls else atm["call_iv"]
    skew_pct = (put_iv_ref - call_iv_ref) * 100.0

    call_oi_sum = sum(s["call_oi"] for s in strikes)
    put_oi_sum = sum(s["put_oi"] for s in strikes)
    pcr_oi = put_oi_sum / call_oi_sum if call_oi_sum > 0 else 1.0

    net_delta_oi = sum(
        s["call_oi"] * s["call_delta"] + s["put_oi"] * s["put_delta"] for s in strikes
    )
    net_gex = sum(s["gamma"] * (s["call_oi"] - s["put_oi"]) for s in strikes)

    cumulative = 0.0
    gamma_flip: float | None = None
    for s in strikes:
        cumulative += s["gamma"] * (s["call_oi"] - s["put_oi"])
        if gamma_flip is None and cumulative > 0:
            gamma_flip = float(s["strike"])

    if net_gex >= 0:
        regime = "POSITIVE_GEX"
    else:
        regime = "NEGATIVE_GEX"

    bias = "NEUTRAL"
    if gamma_flip is not None:
        if spot > gamma_flip and skew_pct > 1.0 and net_delta_oi < 0:
            bias = "BULLISH"
        elif spot < gamma_flip and skew_pct < -1.0 and net_delta_oi > 0:
            bias = "BEARISH"
        elif spot > gamma_flip and net_gex >= 0:
            bias = "BULLISH"
        elif spot < gamma_flip and net_gex < 0:
            bias = "BEARISH"

    return ChainAnalytics(
        spot=spot,
        expiry=expiry,
        atm_strike=atm_strike,
        atm_iv=round(atm_iv, 4),
        skew_pct=round(skew_pct, 2),
        pcr_oi=round(pcr_oi, 2),
        net_delta_oi=round(net_delta_oi, 0),
        net_gex=round(net_gex, 2),
        gamma_flip=gamma_flip,
        regime=regime,
        bias=bias,
    )


def preferred_itm_strikes(spot: float, strike_step: int, direction: str) -> list[int]:
    """Strike preference for S3: 1-step ITM first so premium tracks spot better than ATM."""
    step = max(int(strike_step), 1)
    atm = int(round(spot / step) * step)
    if direction == "LONG":
        # CE: lower strike = ITM
        return [atm - step, atm, atm - 2 * step, atm + step]
    # PE: higher strike = ITM
    return [atm + step, atm, atm + 2 * step, atm - step]


def pick_spot_aligned_option(
    *,
    spot: float,
    chain_rows: list[dict[str, Any]],
    expiry: str,
    direction: str,
    strike_step: int = 50,
    target_delta: float = 0.60,
    delta_min: float = 0.40,
    delta_max: float = 0.75,
    band_points: float = 250.0,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Pick mild-ITM CE (LONG) or PE (SHORT) whose |delta| best tracks spot.

    Prefers strikes that:
      • sit 1-step ITM → ATM (premium moves with Nifty)
      • have |delta| near target (default 0.60, clamped to [delta_min, delta_max])
      • have usable LTP and preferably OI liquidity
    """
    if spot <= 0 or not chain_rows:
        return None

    opt = "CE" if direction == "LONG" else "PE"
    leg_key = "call_options" if direction == "LONG" else "put_options"
    t = years_to_expiry(expiry, now)
    atm = int(round(spot / strike_step) * strike_step)
    candidates: list[dict[str, Any]] = []

    for row in chain_rows:
        try:
            strike = int(float(row.get("strike_price", 0)))
        except (TypeError, ValueError):
            continue
        if abs(strike - spot) > band_points:
            continue
        # Prefer ATM / ITM — skip deep OTM that barely tracks spot.
        if direction == "LONG" and strike > atm + strike_step:
            continue
        if direction == "SHORT" and strike < atm - strike_step:
            continue
        if direction == "LONG" and strike < atm - 2 * strike_step:
            continue  # deep ITM — expensive, lower gamma for +15 premium target
        if direction == "SHORT" and strike > atm + 2 * strike_step:
            continue

        leg = row.get(leg_key) or {}
        instrument_key = str(leg.get("instrument_key") or "")
        if not instrument_key:
            continue
        ltp = _ltp_from_leg(leg)
        oi = _oi_from_leg(leg)

        broker_delta = None
        greeks = leg.get("greeks") or {}
        if isinstance(greeks, dict) and greeks.get("delta") is not None:
            try:
                broker_delta = float(greeks["delta"])
            except (TypeError, ValueError):
                broker_delta = None

        if broker_delta is not None:
            delta = broker_delta
        elif ltp > 0:
            iv = implied_volatility(ltp, spot, strike, t, option_type=opt) or 0.18
            delta = bs_delta(spot, strike, t, iv, option_type=opt)
        else:
            iv = 0.18
            delta = bs_delta(spot, strike, t, iv, option_type=opt)

        abs_delta = abs(delta)
        if abs_delta < delta_min or abs_delta > delta_max:
            if abs(strike - atm) > strike_step:
                continue

        itm_bonus = 0.0
        if direction == "LONG" and strike <= spot:
            # Prefer 1-step ITM over pure ATM so premium tracks Nifty.
            itm_bonus = 0.12 if strike == atm - strike_step else 0.05 if strike == atm else 0.04
        if direction == "SHORT" and strike >= spot:
            itm_bonus = 0.12 if strike == atm + strike_step else 0.05 if strike == atm else 0.04

        atm_dist = abs(strike - atm) / max(strike_step, 1)
        delta_err = abs(abs_delta - target_delta)
        liq_bonus = min(oi / 50_000.0, 0.05) if oi > 0 else 0.0
        score = delta_err + 0.15 * atm_dist - itm_bonus - liq_bonus
        candidates.append(
            {
                "instrument_key": instrument_key,
                "strike": strike,
                "option_type": opt,
                "delta": round(delta, 4),
                "abs_delta": round(abs_delta, 4),
                "ltp": ltp,
                "oi": oi,
                "score": score,
                "expiry": expiry,
            }
        )

    if not candidates:
        for row in chain_rows:
            try:
                strike = int(float(row.get("strike_price", 0)))
            except (TypeError, ValueError):
                continue
            if strike != atm:
                continue
            leg = row.get(leg_key) or {}
            instrument_key = str(leg.get("instrument_key") or "")
            if instrument_key:
                return {
                    "instrument_key": instrument_key,
                    "strike": strike,
                    "option_type": opt,
                    "delta": None,
                    "abs_delta": None,
                    "selection": "atm_fallback",
                    "expiry": expiry,
                }
        return None

    best = min(candidates, key=lambda c: float(c["score"]))
    best["selection"] = "delta_aligned"
    return best
