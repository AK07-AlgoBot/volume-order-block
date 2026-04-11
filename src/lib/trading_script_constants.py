"""
Symbols configured in trading_bot.TRADING_CONFIG["scripts"].
Keep this tuple in sync when adding or removing instruments there.
"""

from __future__ import annotations

# Real broker orders (Upstox); all other symbols in AVAILABLE_SCRIPT_NAMES are paper-only.
LIVE_SCRIPT_NAMES: frozenset[str] = frozenset(
    {
        "NIFTY",
        "BANKNIFTY",
        "SENSEX",
        "CRUDE",
        "GOLDMINI",
        "SILVERMINI",
    }
)


def is_paper_script(script_name: str) -> bool:
    name = (script_name or "").strip().upper()
    return bool(name) and name not in LIVE_SCRIPT_NAMES


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
    "HINDUNILVR",
    "BAJFINANCE",
    "BHARTIARTL",
    "MARUTI",
    "SUNPHARMA",
    "TITAN",
    "ULTRACEMCO",
    "NESTLEIND",
    "POWERGRID",
    "HCLTECH",
    "SIEMENS",
    "UPL",
    "POLYCAB",
    "APOLLOHOSP",
    "BIOCON",
    "MPHASIS",
    "CUMMINSIND",
    "ETERNAL",
    "ADANIPORTS",
)

# Subset for order-block backtests / reports (equity cash names).
ORDER_BLOCK_BACKTEST_SYMBOLS: tuple[str, ...] = (
    "UPL",
    "POLYCAB",
    "APOLLOHOSP",
    "BIOCON",
    "MPHASIS",
    "CUMMINSIND",
    "ETERNAL",
    "ADANIPORTS",
)
