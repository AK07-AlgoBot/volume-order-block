"""Black–Scholes delta for index options (ATM-style risk mapping)."""
from __future__ import annotations

import math


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call_delta(S: float, K: float, T_years: float, sigma: float, r: float = 0.0) -> float:
    """Delta of a European call; for ATM options use S≈K."""
    if S <= 0 or K <= 0:
        return 0.5
    if T_years <= 0 or sigma <= 0:
        return 0.5 if S >= K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T_years) / (sigma * math.sqrt(T_years))
    return norm_cdf(d1)


def bs_put_delta(S: float, K: float, T_years: float, sigma: float, r: float = 0.0) -> float:
    """Delta of a European put (negative for long put)."""
    return bs_call_delta(S, K, T_years, sigma, r) - 1.0


def years_to_expiry_from_ms(expiry_ms: int, now_ms: int | None = None) -> float:
    """Rough year fraction for BS time input."""
    if now_ms is None:
        import time

        now_ms = int(time.time() * 1000)
    sec = max(0.0, (int(expiry_ms) - int(now_ms)) / 1000.0)
    return max(1.0 / (365.25 * 24 * 3600), sec / (365.25 * 24 * 3600.0))
