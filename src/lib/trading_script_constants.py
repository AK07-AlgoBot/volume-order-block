"""
Symbols configured in trading_bot.TRADING_CONFIG["scripts"].
Keep this tuple in sync when adding or removing instruments there.
"""

from __future__ import annotations

AVAILABLE_SCRIPT_NAMES: tuple[str, ...] = (
    "NIFTY",
    "BANKNIFTY",
    "SENSEX",
    "CRUDE",
    "GOLDMINI",
    "SILVERMINI",
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "SBIN",
    "TCS",
    "INFY",
    "AXISBANK",
    "KOTAKBANK",
    "LT",
    "ITC",
)
