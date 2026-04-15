"""
Multi-Script Trading Bot with EMA Crossover Strategy
Version: 2.0
Created: March 4, 2026
"""

import copy
import threading
import time
import math
import logging
from typing import Any
import json
import sys
import os
import html
import atexit
import gzip
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (_REPO_ROOT, _REPO_ROOT / "src" / "lib", _REPO_ROOT / "src" / "bot"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import pandas as pd
import numpy as np
import requests
from colorama import Fore, Style, init

from upstox_credentials_store import (
    DEFAULT_BASE_URL,
    credentials_file_for_user,
    load_upstox_credentials_for_user,
    mask_tail,
    sanitize_username,
    user_data_dir,
)
from trading_preferences_store import read_trading_preferences
from trading_script_constants import is_paper_script
from zerodha_credentials_store import load_zerodha_credentials_for_user

from kite_fut_instrument import resolve_kite_instrument_token
from kite_rest_candles import (
    default_swing_window,
    fetch_historical_raw,
    kite_candles_to_dataframe,
    map_bot_interval_to_kite,
)
from kite_tick_stream import KiteTickStream

from option_greeks import (
    bs_call_delta,
    bs_put_delta,
    years_to_expiry_from_ms,
)

# Initialize colorama
init(autoreset=True)

# ============================================================================
# TELEGRAM NOTIFICATIONS
# ============================================================================

TELEGRAM_BOT_TOKEN = "8376419713:AAENJb_Rta0qBA1ypZsHZvkfOqSWTGP256Y"
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
TELEGRAM_GROUP_CHAT_ID = -5105991026


def telegram_notifications_enabled_for_user(username: str) -> bool:
    """Telegram alerts for the single dashboard account."""
    return sanitize_username(username) == "AK07"


# Dashboard API (override base URL in Docker: DASHBOARD_API_BASE=http://api:8000)
DASHBOARD_CONFIG = {
    "enabled": True,
    "base_url": "http://localhost:8000",
    "timeout_seconds": 2.0,
    "batch_size": 50,
}
_dash_api_base = os.environ.get("DASHBOARD_API_BASE", "").strip()
if _dash_api_base:
    DASHBOARD_CONFIG = {**DASHBOARD_CONFIG, "base_url": _dash_api_base.rstrip("/")}

MCX_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"
NSE_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
BSE_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz"


def send_trade_notification(trade: dict, chat_id: int | str = None) -> bool:
    """
    Send a trade dict to a Telegram group.

    Expected trade format:
    {
        "symbol": str,
        "action": str,      # "BUY"/"SELL"
        "quantity": float | int,
        "price": float | int,
        "timestamp": datetime | str
    }

    Returns True on success, False on failure.
    """
    chat_id = chat_id or TELEGRAM_GROUP_CHAT_ID

    symbol = trade.get("symbol")
    action = trade.get("action")
    quantity = trade.get("quantity")
    price = trade.get("price")
    reason = str(trade.get("reason") or "").upper()
    stop_loss = trade.get("stop_loss")
    target_price = trade.get("target_price")
    realized_pnl = trade.get("realized_pnl")
    win_percent = trade.get("win_percent")
    chart_percent = trade.get("chart_percent")
    chart_volume = trade.get("chart_volume")
    entry_adx = trade.get("entry_adx")
    entry_plus_di = trade.get("entry_plus_di")
    entry_minus_di = trade.get("entry_minus_di")
    error_text = trade.get("error_text")
    endpoint = trade.get("endpoint")
    note = trade.get("note")
    timestamp = trade.get("timestamp")

    if isinstance(timestamp, datetime):
        ts_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")
    else:
        ts_str = str(timestamp)

    entry_reasons = {"EMA_CROSSOVER"}
    exit_reasons = {
        "STOP_LOSS_HIT",
        "TRAILING_STOP_LOSS_HIT",
        "TARGET_HIT",
        "OB_ZONE_BREACH",
        "OPPOSITE_CROSSOVER",
        "EOD_SQUAREOFF",
        "PORTFOLIO_STOP_LOSS",
        "PORTFOLIO_STOP",
    }

    note_upper = str(note or "").upper()

    if reason == "TRAILING_SL_UPDATED":
        title = "🟣 *Trailing SL Updated*"
    elif reason in exit_reasons:
        title = "🔴 *Trade Closed*"
    elif reason == "ORDER_FAILED":
        if "CLOSE MANUALLY" in note_upper:
            title = "🟠 *Manual EXIT Required*"
        else:
            title = "🟠 *Manual ENTRY Required*"
    elif reason in entry_reasons:
        title = "🟢 *New Trade Executed*"
    else:
        title = "✅ *Trade Update*"

    acct = str(trade.get("account") or "").strip()
    acct_line = f"*Account*: `{acct}`\n" if acct else ""

    message = (
        f"{title}\n"
        f"{acct_line}"
        f"*Symbol*: `{symbol}`\n"
        f"*Action*: *{str(action).upper()}*\n"
        f"*Quantity*: `{quantity}`\n"
        f"*Price*: `{price}`\n"
        + (f"\n*Reason*: `{reason}`" if reason else "")
        + (f"\n*SL*: `{float(stop_loss):.2f}`" if stop_loss is not None else "")
        + (f"\n*Target*: `{float(target_price):.2f}`" if target_price is not None else "")
        + (f"\n*Chart %*: `{float(chart_percent):.2f}%`" if chart_percent is not None else "")
        + (f"\n*Chart Vol*: `{float(chart_volume):.0f}`" if chart_volume is not None else "")
        + (f"\n*Win %*: `{float(win_percent):.1f}%`" if win_percent is not None else "")
        + (f"\n*Trade P&L*: `{float(realized_pnl):.2f}`" if realized_pnl is not None else "")
        + (f"\n*ADX*: `{float(entry_adx):.2f}`" if entry_adx is not None else "")
        + (f"\n*+DI*: `{float(entry_plus_di):.2f}`" if entry_plus_di is not None else "")
        + (f"\n*-DI*: `{float(entry_minus_di):.2f}`" if entry_minus_di is not None else "")
        + (f"\n*Note*: `{note}`" if note else "")
        + (f"\n*Error*: `{error_text}`" if error_text else "")
        + (f"\n*Endpoint*: `{endpoint}`" if endpoint else "")
        + "\n"
        f"*Time*: `{ts_str}`"
    )

    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }

    try:
        resp = requests.post(TELEGRAM_API_URL, json=payload, timeout=10)
        return resp.ok
    except Exception as e:
        logging.getLogger(__name__).error(f"Failed to send Telegram trade notification: {e}")
        return False


def send_paper_trade_notification(trade: dict, is_entry: bool, chat_id: int | str = None) -> bool:
    """
    Paper trades: Telegram HTML with blockquote (amber cue via emoji; no true per-message background in Bot API).
    """
    chat_id = chat_id or TELEGRAM_GROUP_CHAT_ID
    symbol = html.escape(str(trade.get("symbol") or ""))
    action = html.escape(str(trade.get("action") or "").upper())
    quantity = trade.get("quantity")
    price_raw = trade.get("price")
    price_txt = html.escape(f"{float(price_raw):.2f}" if price_raw is not None else "")
    reason = html.escape(str(trade.get("reason") or "").upper())
    stop_loss = trade.get("stop_loss")
    target_price = trade.get("target_price")
    realized_pnl = trade.get("realized_pnl")
    win_percent = trade.get("win_percent")
    chart_percent = trade.get("chart_percent")
    chart_volume = trade.get("chart_volume")
    entry_adx = trade.get("entry_adx")
    entry_plus_di = trade.get("entry_plus_di")
    entry_minus_di = trade.get("entry_minus_di")
    note = trade.get("note")
    timestamp = trade.get("timestamp")
    acct = html.escape(str(trade.get("account") or "").strip())

    if isinstance(timestamp, datetime):
        ts_str = html.escape(timestamp.strftime("%Y-%m-%d %H:%M:%S"))
    else:
        ts_str = html.escape(str(timestamp))

    label = "PAPER ENTRY" if is_entry else "PAPER EXIT"
    header = f"🟨 <b>{html.escape(label)}</b>"

    block_parts: list[str] = [
        f"<b>Symbol</b>: <code>{symbol}</code>",
        f"<b>Action</b>: <b>{action}</b>",
        f"<b>Qty</b>: <code>{html.escape(str(quantity))}</code>",
        f"<b>Price</b>: <code>{price_txt}</code>",
    ]
    if acct:
        block_parts.insert(0, f"<b>Account</b>: <code>{acct}</code>")
    if reason:
        block_parts.append(f"<b>Reason</b>: <code>{reason}</code>")
    if stop_loss is not None:
        block_parts.append(f"<b>SL</b>: <code>{float(stop_loss):.2f}</code>")
    if target_price is not None:
        block_parts.append(f"<b>Target</b>: <code>{float(target_price):.2f}</code>")
    if chart_percent is not None:
        block_parts.append(f"<b>Chart %</b>: <code>{float(chart_percent):.2f}%</code>")
    if chart_volume is not None:
        block_parts.append(f"<b>Chart Vol</b>: <code>{float(chart_volume):.0f}</code>")
    if win_percent is not None:
        block_parts.append(f"<b>Win %</b>: <code>{float(win_percent):.1f}%</code>")
    if realized_pnl is not None:
        block_parts.append(f"<b>Trade P&amp;L</b>: <code>{float(realized_pnl):.2f}</code>")
    if entry_adx is not None:
        block_parts.append(f"<b>ADX</b>: <code>{float(entry_adx):.2f}</code>")
    if entry_plus_di is not None:
        block_parts.append(f"<b>+DI</b>: <code>{float(entry_plus_di):.2f}</code>")
    if entry_minus_di is not None:
        block_parts.append(f"<b>−DI</b>: <code>{float(entry_minus_di):.2f}</code>")
    if note:
        block_parts.append(f"<b>Note</b>: {html.escape(str(note))}")
    block_parts.append(f"<b>Time</b>: <code>{ts_str}</code>")

    message = f"{header}\n\n<blockquote>{chr(10).join(block_parts)}</blockquote>"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(TELEGRAM_API_URL, json=payload, timeout=10)
        return resp.ok
    except Exception as e:
        logging.getLogger(__name__).error(f"Failed to send paper Telegram notification: {e}")
        return False


def send_telegram_test_message(message: str = "Hi from VOLUME-ORDER-BLOCK bot") -> bool:
    """
    Send a simple test message to the configured Telegram group.
    Returns True on success, False on failure.
    """
    payload = {
        "chat_id": TELEGRAM_GROUP_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }

    try:
        resp = requests.post(TELEGRAM_API_URL, json=payload, timeout=10)
        if not resp.ok:
            logging.getLogger(__name__).error(
                f"Failed to send Telegram test message: {resp.status_code} {resp.text}"
            )
        return resp.ok
    except Exception as e:
        logging.getLogger(__name__).error(f"Error sending Telegram test message: {e}")
        return False


class DashboardClient:
    """Thin client for dashboard API with batch update support."""

    def __init__(
        self,
        trading_user: str,
        enabled=True,
        base_url="http://localhost:8000",
        timeout_seconds=2.0,
    ):
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self._logger = logging.getLogger(__name__)
        self._bot_token = os.environ.get("BOT_API_TOKEN", "").strip()
        self._trading_user = sanitize_username(trading_user)

    def _post_json(self, endpoint, payload):
        if not self.enabled:
            return True

        url = f"{self.base_url}{endpoint}"
        headers = {"X-Trading-User": self._trading_user}
        if self._bot_token:
            headers["X-Bot-Token"] = self._bot_token
        try:
            response = self.session.post(
                url, json=payload, timeout=self.timeout_seconds, headers=headers
            )
            if not response.ok:
                self._logger.error(
                    f"Dashboard API failed [{endpoint}] {response.status_code}: {response.text[:300]}"
                )
                return False
            return True
        except Exception as e:
            self._logger.error(f"Dashboard API error [{endpoint}]: {e}")
            return False

    def post_trade_open(self, trade):
        return self._post_json("/api/trade/open", trade)

    def post_trade_update(self, trade):
        return self._post_json("/api/trade/update", trade)

    def post_trade_update_batch(self, trades):
        if not trades:
            return True
        return self._post_json("/api/trades/update-batch", trades)

    def post_trade_close(self, trade):
        return self._post_json("/api/trade/close", trade)

# ============================================================================
# CONFIGURATION
# ============================================================================

# Upstox API: use dashboard → Upstox settings, or upstox_credentials.json, or UPSTOX_* env vars.
API_CONFIG = {
    "base_url": DEFAULT_BASE_URL,
}

# Runtime override for daily shutdown (IST), format HH:MM.
DAILY_SHUTDOWN_TIME = (os.environ.get("TRADING_DAILY_SHUTDOWN_TIME") or "23:21").strip() or "23:21"

# Trading Configuration
TRADING_CONFIG = {
    "scripts": {
        "NIFTY": "NSE_FO|51714",           # NIFTY Futures for data fetching
        "BANKNIFTY": "NSE_FO|51701",       # BANKNIFTY Futures for data fetching
        "SENSEX": "BSE_FO|825565",         # SENSEX Futures for data fetching
        "CRUDE": "MCX_FO|486502",
        "GOLDMINI": "MCX_FO|487665",
        "SILVERMINI": "MCX_FO|457533",
        # Liquid Nifty-50 stock futures (auto-resolved to current FO contract at runtime).
        "RELIANCE": "",
        "HDFCBANK": "",
        "ICICIBANK": "",
        "SBIN": "",
        "TCS": "",
        "INFY": "",
        "AXISBANK": "",
        "KOTAKBANK": "",
        "LT": "",
        "ITC": "",
        "HINDUNILVR": "",
        "BAJFINANCE": "",
        "BHARTIARTL": "",
        "MARUTI": "",
        "SUNPHARMA": "",
        "TITAN": "",
        "ULTRACEMCO": "",
        "NESTLEIND": "",
        "POWERGRID": "",
        "HCLTECH": "",
        "SIEMENS": "",
        "UPL": "",
        "POLYCAB": "",
        "APOLLOHOSP": "",
        "BIOCON": "",
        "MPHASIS": "",
        "CUMMINSIND": "",
        "ETERNAL": "",
        "ADANIPORTS": "",
    },
    # Separate tokens for order placement (FUTURES/COMMODITIES)
    "order_tokens": {
        "NIFTY": "NSE_FO|51714",
        "BANKNIFTY": "NSE_FO|51701",
        "SENSEX": "BSE_FO|825565",
        "CRUDE": "MCX_FO|486502",
        "GOLDMINI": "MCX_FO|487665",
        "SILVERMINI": "MCX_FO|457533",
        "RELIANCE": "",
        "HDFCBANK": "",
        "ICICIBANK": "",
        "SBIN": "",
        "TCS": "",
        "INFY": "",
        "AXISBANK": "",
        "KOTAKBANK": "",
        "LT": "",
        "ITC": "",
        "HINDUNILVR": "",
        "BAJFINANCE": "",
        "BHARTIARTL": "",
        "MARUTI": "",
        "SUNPHARMA": "",
        "TITAN": "",
        "ULTRACEMCO": "",
        "NESTLEIND": "",
        "POWERGRID": "",
        "HCLTECH": "",
        "SIEMENS": "",
        "UPL": "",
        "POLYCAB": "",
        "APOLLOHOSP": "",
        "BIOCON": "",
        "MPHASIS": "",
        "CUMMINSIND": "",
        "ETERNAL": "",
        "ADANIPORTS": "",
    },
    # Exchange contract size (shares per lot) — Upstox NFO/BSE FO index standards:
    # NIFTY 1 lot = 65 qty, BANKNIFTY 1 lot = 30, SENSEX 1 lot = 20.
    # Order quantity sent to API = quantity (lots below) × lot_sizes[script].
    "lot_sizes": {
        "NIFTY": 65,
        "BANKNIFTY": 30,
        "SENSEX": 20,
        "CRUDE": 100,
        "GOLDMINI": 1,
        "SILVERMINI": 5,
        # 0 means "auto-discover from instrument master" during startup.
        "RELIANCE": 0,
        "HDFCBANK": 0,
        "ICICIBANK": 0,
        "SBIN": 0,
        "TCS": 0,
        "INFY": 0,
        "AXISBANK": 0,
        "KOTAKBANK": 0,
        "LT": 0,
        "ITC": 0,
        "HINDUNILVR": 0,
        "BAJFINANCE": 0,
        "BHARTIARTL": 0,
        "MARUTI": 0,
        "SUNPHARMA": 0,
        "TITAN": 0,
        "ULTRACEMCO": 0,
        "NESTLEIND": 0,
        "POWERGRID": 0,
        "HCLTECH": 0,
        "SIEMENS": 0,
        "UPL": 0,
        "POLYCAB": 0,
        "APOLLOHOSP": 0,
        "BIOCON": 0,
        "MPHASIS": 0,
        "CUMMINSIND": 0,
        "ETERNAL": 0,
        "ADANIPORTS": 0,
    },
    # Market data: "kite" (Kite REST candles + WebSocket LTP) | "upstox" (poll Upstox REST). Orders: always Upstox.
    # Override at runtime: MARKET_DATA_PROVIDER=upstox | kite
    "market_data_provider": "kite",
    # Optional { "NIFTY": 12345678 } if auto token resolve fails (Kite instruments CSV token).
    "kite_instrument_token_overrides": {},
    "interval": "1minute",  # Base candle interval (Kite: minute/5minute/…; Upstox: 1minute)
    "signal_interval": "5minute",  # Strategy timeframe (EMA runs on 5-minute candles)
    "ema_short": 5,
    "ema_long": 18,
    "portfolio_stop_loss": 10000,  # ₹10,000
    "trailing_stop_loss_percent": 1.0,  # 1%
    "trail_step_percent": 0.5,  # After 1:1, trail SL by 0.5% for every 0.5% favorable move
    # Profit-lock ladder in R-multiples.
    # trigger_r: when trade reaches this R, lock_r: guaranteed R to retain in SL.
    "profit_lock_ladder": [
        {"trigger_r": 1.0, "lock_r": 0.25},
        {"trigger_r": 1.5, "lock_r": 0.75},
        {"trigger_r": 2.0, "lock_r": 1.25},
        {"trigger_r": 2.5, "lock_r": 1.75},
    ],
    "target_percent": 2.0,  # Book profit at +2% move (or -2% for SELL)
    "trailing_overrides_by_script": {
        "CRUDE": {
            "breakeven_trigger_percent": 1.0,
            "trail_step_percent": 0.2
        },
        "SILVERMINI": {
            "breakeven_trigger_percent": 1.0,
            "trail_step_percent": 0.2
        },
        "NIFTY": {
            "breakeven_trigger_percent": 1.0,
            "trail_step_percent": 0.2
        },
        "BANKNIFTY": {
            "breakeven_trigger_percent": 1.0,
            "trail_step_percent": 0.2
        },
        "SENSEX": {
            "breakeven_trigger_percent": 1.0,
            "trail_step_percent": 0.2
        }
    },
    # Explicitly apply the same profit-lock ladder profile as CRUDE.
    "profit_lock_ladder_by_script": {
        "CRUDE": [
            {"trigger_r": 1.0, "lock_r": 0.25},
            {"trigger_r": 1.5, "lock_r": 0.75},
            {"trigger_r": 2.0, "lock_r": 1.25},
            {"trigger_r": 2.5, "lock_r": 1.75}
        ],
        "SILVERMINI": [
            {"trigger_r": 1.0, "lock_r": 0.25},
            {"trigger_r": 1.5, "lock_r": 0.75},
            {"trigger_r": 2.0, "lock_r": 1.25},
            {"trigger_r": 2.5, "lock_r": 1.75}
        ],
        "NIFTY": [
            {"trigger_r": 1.0, "lock_r": 0.25},
            {"trigger_r": 1.5, "lock_r": 0.75},
            {"trigger_r": 2.0, "lock_r": 1.25},
            {"trigger_r": 2.5, "lock_r": 1.75}
        ],
        "BANKNIFTY": [
            {"trigger_r": 1.0, "lock_r": 0.25},
            {"trigger_r": 1.5, "lock_r": 0.75},
            {"trigger_r": 2.0, "lock_r": 1.25},
            {"trigger_r": 2.5, "lock_r": 1.75}
        ],
        "SENSEX": [
            {"trigger_r": 1.0, "lock_r": 0.25},
            {"trigger_r": 1.5, "lock_r": 0.75},
            {"trigger_r": 2.0, "lock_r": 1.25},
            {"trigger_r": 2.5, "lock_r": 1.75}
        ]
    },
    "min_ob_percent_by_script": {
        "NIFTY": 0.44,
        "BANKNIFTY": 0.26,
        "SENSEX": 0.11,
        "CRUDE": 0.60,
        "GOLDMINI": 0.20,
        "SILVERMINI": 0.55
    },
    # Minimum EMA5-EMA18 gap as % of price at crossover — blocks flat/choppy crossovers
    "min_ema_separation_percent": 0.03,
    "min_ema_separation_percent_by_script": {
        "NIFTY": 0.03,
        "BANKNIFTY": 0.03,
        "SENSEX": 0.03,
        "CRUDE": 0.03,
        "GOLDMINI": 0.03,
        "SILVERMINI": 0.03
    },
    # ADX trend-strength gate for new entries.
    "adx_filter_enabled": True,
    "adx_period": 14,
    "adx_min_threshold": 20.0,
    "adx_min_threshold_by_script": {
        "NIFTY": 20.0,
        "BANKNIFTY": 20.0,
        "SENSEX": 20.0,
        "CRUDE": 22.0,
        "GOLDMINI": 20.0,
        "SILVERMINI": 22.0
    },
    # Heuristic confidence score (0-100) logged as trade_prob for ENTRY/SKIP analysis.
    "trade_probability_weights": {
        "ema_slope": 0.25,
        "ema_sep": 0.25,
        "ob_quality": 0.30,
        "level_proximity": 0.20
    },
    "trade_probability_reference_level_percent": 33.66,
    "order_block_lookback_candles": 12,  # Search depth for latest opposite candle (5m) as order block
    "chart_ob_max_active_per_side": 15,  # Match TradingView array cap per side (15)
    # NSE-segment rupee money-lock overlay (indices + NSE-listed FO names in segment_scripts["NSE"]):
    # - At trigger_pnl, lock first lock_increment_pnl above cost.
    # - For every step_pnl extra MFE, lock one more lock_increment_pnl.
    # Omit "scripts" to use all names under segment_scripts["NSE"].
    "nse_money_lock": {
        "enabled": True,
        "trigger_pnl": 3000.0,
        "step_pnl": 500.0,
        "lock_increment_pnl": 500.0
    },
    # NSE per-trade rupee exits (applies to scripts in segment_scripts["NSE"] unless overridden).
    # Trailing SL behavior remains unchanged; this only sets initial SL/target placement.
    "nse_trade_pnl_levels": {
        "enabled": True,
        "target_pnl": 5000.0,
        "stop_loss_pnl": 3000.0
    },
    # How often the bot loop runs (LTP from Kite ticks is read each loop). Lower = snappier exits; more REST/API load.
    # Override: BOT_LOOP_INTERVAL_SEC=5
    "loop_interval": 10,  # seconds between each check
    "contract_roll_retry_seconds": 300,  # seconds between roll attempts per script
    "contract_roll_mcx_cache_seconds": 21600,  # 6h MCX instrument cache window
    "quantity": 1,  # Futures lots per order (e.g. 1 lot NIFTY → 1×65 = 65 quantity on Upstox)
    # Optional per-script exchange quantity override for order placement.
    # Example: CRUDE quantity=1 (instead of lots*lot_size) to match broker setup.
    "order_quantity_override_by_script": {
        "CRUDE": 1
    },
    # Options companion strategy (for NSE index futures entries).
    "options_enabled": True,
    "options_scripts": ["NIFTY", "BANKNIFTY", "SENSEX"],
    "options_total_lots": 4,
    "options_target3_r": 3.0,
    "options_gtt_enabled": True,
    "options_breakeven_buffer_points": 0.0,
    # IST time after which no new option entries on that contract's expiry day.
    "options_expiry_day_cutoff_ist": "12:00",
    # Hybrid option SL: map futures-defined risk to premium using assumed ATM delta,
    # with a floor as % of entry premium (no broker Greeks required).
    "options_sl_delta_assumption": 0.5,
    "options_sl_min_premium_floor_ratio": 0.35,
    # Scale NSE rupee target (nse_trade_pnl_levels.target_pnl) to option leg by lots vs futures lots.
    "options_rupee_profit_booking": {
        "enabled": True,
        "lot_fraction": 0.25,
        "futures_lots_reference": None,
    },
    # ladder_gtt: after fill, place SL + TP GTTs on OPTION premium (1:1 / 1:2 / 1:3 lot splits).
    # legacy_underlying: prior behaviour (underlying R-level partials + single SL GTT).
    "options_exit_mode": "ladder_gtt",
    "options_tp_lot_splits": [2, 1, 1],
    "options_use_bs_delta_for_r": True,
    "options_iv_annual": 0.18,
    "options_risk_free_rate": 0.0,
    "options_chart_crossover_exit": True,
    "options_crossover_interval": "5minute",
    "segment_scripts": {
        "NSE": [
            "NIFTY", "BANKNIFTY", "SENSEX",
            "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "TCS",
            "INFY", "AXISBANK", "KOTAKBANK", "LT", "ITC",
            "HINDUNILVR", "BAJFINANCE", "BHARTIARTL", "MARUTI", "SUNPHARMA",
            "TITAN", "ULTRACEMCO", "NESTLEIND", "POWERGRID", "HCLTECH",
            "SIEMENS", "UPL", "POLYCAB", "APOLLOHOSP", "BIOCON", "MPHASIS",
            "CUMMINSIND", "ETERNAL", "ADANIPORTS",
        ],
        "MCX": ["CRUDE", "GOLDMINI", "SILVERMINI"]
    },
    "entry_start_times": {
        "NSE": "09:25",
        "MCX": "09:10"
    },
    "eod_squareoff_times": {
        "NSE": "15:20",
        "MCX": "23:20"
    },
    "daily_shutdown_time": DAILY_SHUTDOWN_TIME,
    "auto_archive_on_shutdown": True
}


def runtime_trading_config() -> dict:
    """Deep copy of TRADING_CONFIG with optional env overrides (used by main(); does not mutate TRADING_CONFIG)."""
    cfg = copy.deepcopy(TRADING_CONFIG)
    md = os.environ.get("MARKET_DATA_PROVIDER", "").strip().lower()
    if md in ("kite", "upstox"):
        cfg["market_data_provider"] = md
    li = os.environ.get("BOT_LOOP_INTERVAL_SEC", "").strip()
    if li:
        try:
            v = int(li)
            if v >= 1:
                cfg["loop_interval"] = v
        except ValueError:
            pass
    # Kite: slower REST/candle refresh while SL/target can fire on WebSocket LTP (see KITE_STREAM_DRIVE_EXITS).
    ks = os.environ.get("KITE_STRATEGY_LOOP_SEC", "").strip()
    if ks and str(cfg.get("market_data_provider", "")).strip().lower() == "kite":
        try:
            v = int(ks)
            if v >= 1:
                cfg["loop_interval"] = v
        except ValueError:
            pass
    return cfg


REPO_ROOT = _REPO_ROOT
LOCK_FILE = REPO_ROOT / "src" / "bot" / "trading_bot.lock"

# Console-only bootstrap; per-account file loggers are attached in TradingBot.__init__
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Index futures where contract roll / expiry-day next-serial rules apply.
_INDEX_FO_SCRIPT_NAMES = frozenset({"NIFTY", "BANKNIFTY", "SENSEX"})


def _ist_date_from_expiry_ms(expiry_ms: int) -> date:
    return datetime.fromtimestamp(expiry_ms / 1000.0, tz=ZoneInfo("Asia/Kolkata")).date()


def _is_last_thursday_of_month_ist(expiry_ms: int) -> bool:
    """NSE/BSE monthly index derivatives typically expire on the last Thursday."""
    d = _ist_date_from_expiry_ms(expiry_ms)
    if d.weekday() != 3:
        return False
    nxt = d + timedelta(days=7)
    return nxt.month != d.month


# ============================================================================
# UPSTOX API CLIENT
# ============================================================================

class UpstoxClient:
    """Upstox API v2 Client for market data and orders"""

    def __init__(self, access_token, base_url, username: str, log: logging.Logger | None = None):
        self._username = sanitize_username(username)
        self._log = log or logger
        self.access_token = access_token or ""
        self.base_url = base_url or DEFAULT_BASE_URL
        self.session = requests.Session()
        self.set_access_token(self.access_token)

    def set_access_token(self, access_token: str) -> None:
        self.access_token = access_token or ""
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
            }
        )

    def refresh_credentials_if_changed(self) -> None:
        creds = load_upstox_credentials_for_user(self._username)
        token = (creds.get("access_token") or "").strip()
        if token and token != self.access_token:
            self._log.info(
                "Reloading Upstox access token from disk (%s)",
                credentials_file_for_user(self._username).name,
            )
            self.set_access_token(token)
    
    def get_user_profile(self):
        """Get user profile information"""
        try:
            url = f"{self.base_url}/user/profile"
            response = self.session.get(url, timeout=30)
            if response.status_code != 200:
                snippet = (response.text or "").replace("\n", " ")[:500]
                self._log.error(
                    f"Upstox user profile HTTP {response.status_code}: {snippet or '(empty body)'}"
                )
                return None
            data = response.json()
            return data.get('data', {})
        except Exception as e:
            self._log.error(f"Error fetching user profile: {e}")
            return None
    
    def get_historical_candles(self, instrument_key, interval, from_date, to_date):
        """Get historical candle data"""
        try:
            url = f"{self.base_url}/historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}"
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
            
            if data.get('status') == 'success':
                candles = data.get('data', {}).get('candles', [])
                if candles:
                    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'])
                    df['timestamp'] = pd.to_datetime(df['timestamp'])
                    df = df.sort_values('timestamp').reset_index(drop=True)
                    return df
            return None
        except Exception as e:
            self._log.error(f"Error fetching historical candles: {e}")
            return None

    def get_intraday_candles(self, instrument_key, interval):
        """Get intraday candle data"""
        try:
            url = f"{self.base_url}/historical-candle/intraday/{instrument_key}/{interval}"
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
            
            if data.get('status') == 'success':
                candles = data.get('data', {}).get('candles', [])
                if candles:
                    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'])
                    df['timestamp'] = pd.to_datetime(df['timestamp'])
                    df = df.sort_values('timestamp').reset_index(drop=True)
                    return df
            return None
        except Exception as e:
            self._log.error(f"Error fetching intraday candles: {e}")
            return None

    def place_order(self, instrument_key, quantity, transaction_type, order_type="MARKET", price=None):
        """Place an order"""
        payload = {
            "quantity": quantity,
            "product": "I",  # Intraday for futures/commodities
            "validity": "DAY",
            "price": price if price else 0,
            "tag": "trading_bot",
            "instrument_token": instrument_key,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False
        }

        if instrument_key.startswith(("NSE_", "BSE_")):
            endpoint_candidates = [
                f"{self.base_url}/order/place",
                "https://api-hft.upstox.com/v2/order/place",
            ]
        else:
            endpoint_candidates = [
                "https://api-hft.upstox.com/v2/order/place",
                f"{self.base_url}/order/place",
            ]

        last_error = "Unknown order placement error"
        last_endpoint = ""

        for url in endpoint_candidates:
            last_endpoint = url
            try:
                response = self.session.post(url, json=payload)
                response_data = response.json() if response.text else {}

                if response.status_code == 200 and response_data.get('status') == 'success':
                    self._log.info(
                        f" Order placed via {url}: {transaction_type} {quantity} of {instrument_key}"
                    )
                    return {
                        "status": "success",
                        "data": response_data.get('data', {}),
                        "endpoint": url
                    }

                broker_error = ""
                if isinstance(response_data, dict):
                    errors = response_data.get('errors') or []
                    if errors and isinstance(errors, list):
                        first = errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}
                        broker_error = first.get('message') or str(first)
                    broker_error = broker_error or response_data.get('message', '')

                last_error = (
                    f"HTTP {response.status_code} - {broker_error or response.text[:250]}"
                )
            except Exception as e:
                last_error = str(e)

        self._log.error(
            f"ERROR: Order failed on all endpoints for {instrument_key} {transaction_type} qty={quantity}. "
            f"Last endpoint={last_endpoint}, error={last_error}"
        )
        return {
            "status": "error",
            "error": last_error,
            "endpoint": last_endpoint
        }

    def get_ltp(self, instrument_key):
        """Fetch latest traded price for any instrument key."""
        try:
            url = f"{self.base_url}/market-quote/ltp"
            response = self.session.get(url, params={"instrument_key": instrument_key}, timeout=20)
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success":
                return None
            data = payload.get("data", {}) or {}
            row = data.get(instrument_key, {}) or {}
            ltp = row.get("last_price")
            return float(ltp) if ltp is not None else None
        except Exception as e:
            self._log.warning(f"LTP fetch failed for {instrument_key}: {e}")
            return None

    def place_gtt_order(
        self,
        instrument_key: str,
        quantity: int,
        transaction_type: str,
        trigger_price: float,
        limit_price: float | None = None,
        tag: str = "trading_bot_opt",
    ) -> dict[str, Any]:
        """
        Best-effort GTT placement.
        Upstox API variants differ by account; this method returns error details on failure.
        """
        payload = {
            "type": "single",
            "condition": {
                "instrument_token": instrument_key,
                "trigger_price": float(trigger_price),
            },
            "orders": [
                {
                    "transaction_type": transaction_type,
                    "quantity": int(quantity),
                    "order_type": "LIMIT" if limit_price is not None else "MARKET",
                    "price": float(limit_price) if limit_price is not None else 0,
                    "product": "I",
                    "validity": "DAY",
                    "disclosed_quantity": 0,
                    "trigger_price": 0,
                    "tag": tag,
                }
            ],
        }
        endpoints = [
            f"{self.base_url}/order/gtt/place",
            f"{self.base_url}/gtt/order/place",
        ]
        last_error = "Unknown GTT placement error"
        last_endpoint = ""
        for url in endpoints:
            last_endpoint = url
            try:
                response = self.session.post(url, json=payload, timeout=20)
                body = response.json() if response.text else {}
                if response.status_code == 200 and body.get("status") == "success":
                    return {"status": "success", "data": body.get("data", {}), "endpoint": url}
                msg = body.get("message", "") if isinstance(body, dict) else ""
                last_error = f"HTTP {response.status_code} - {msg or response.text[:250]}"
            except Exception as e:
                last_error = str(e)
        return {"status": "error", "error": last_error, "endpoint": last_endpoint}

    def cancel_gtt_order(self, gtt_id: str) -> dict[str, Any]:
        endpoints = [
            f"{self.base_url}/order/gtt/cancel/{gtt_id}",
            f"{self.base_url}/gtt/order/cancel/{gtt_id}",
        ]
        last_error = "Unknown GTT cancel error"
        last_endpoint = ""
        for url in endpoints:
            last_endpoint = url
            try:
                response = self.session.delete(url, timeout=20)
                body = response.json() if response.text else {}
                if response.status_code == 200 and body.get("status") == "success":
                    return {"status": "success", "data": body.get("data", {}), "endpoint": url}
                msg = body.get("message", "") if isinstance(body, dict) else ""
                last_error = f"HTTP {response.status_code} - {msg or response.text[:250]}"
            except Exception as e:
                last_error = str(e)
        return {"status": "error", "error": last_error, "endpoint": last_endpoint}

    def get_short_term_positions(self) -> list[dict]:
        """Net quantities per instrument (best-effort for GTT ladder reconciliation)."""
        url = f"{self.base_url}/portfolio/short-term-positions"
        try:
            response = self.session.get(url, timeout=25)
            if response.status_code != 200:
                return []
            body = response.json() if response.text else {}
            if body.get("status") != "success":
                return []
            data = body.get("data") or []
            return data if isinstance(data, list) else []
        except Exception as e:
            self._log.warning(f"short-term-positions failed: {e}")
            return []

    def get_order_average_price(self, order_id: str) -> float | None:
        """Average fill price for a completed order (best-effort)."""
        oid = str(order_id or "").strip()
        if not oid:
            return None
        try:
            response = self.session.get(
                f"{self.base_url}/order/trades", params={"order_id": oid}, timeout=20
            )
            if response.status_code != 200:
                return None
            body = response.json() if response.text else {}
            if body.get("status") != "success":
                return None
            rows = body.get("data") or []
            if not isinstance(rows, list):
                return None
            pv = 0.0
            qv = 0.0
            for row in rows:
                pr = row.get("average_price") or row.get("price")
                q = row.get("quantity") or row.get("fill_quantity") or row.get("filled_quantity")
                if pr is None or q is None:
                    continue
                try:
                    pv += float(pr) * abs(float(q))
                    qv += abs(float(q))
                except (TypeError, ValueError):
                    continue
            if qv > 0:
                return pv / qv
        except Exception as e:
            self._log.debug(f"order/trades failed for {oid}: {e}")
        return None

# ============================================================================
# TECHNICAL ANALYSIS
# ============================================================================

class TechnicalAnalyzer:
    """Calculate technical indicators"""
    
    @staticmethod
    def calculate_ema(series, period):
        """Calculate Exponential Moving Average"""
        return series.ewm(span=period, adjust=False).mean()
    
    @staticmethod
    def calculate_signals(df, short_period=5, long_period=18):
        """Calculate EMA crossover signals"""
        if df is None or len(df) < long_period:
            return None
        
        df = df.copy()
        df['ema_short'] = TechnicalAnalyzer.calculate_ema(df['close'], short_period)
        df['ema_long'] = TechnicalAnalyzer.calculate_ema(df['close'], long_period)
        
        # Generate signals
        df['signal'] = 0
        df.loc[df['ema_short'] > df['ema_long'], 'signal'] = 1  # Buy
        df.loc[df['ema_short'] < df['ema_long'], 'signal'] = -1  # Sell
        
        # Detect crossovers
        df['prev_signal'] = df['signal'].shift(1)
        df['crossover'] = (df['signal'] != df['prev_signal']) & (df['prev_signal'] != 0)
        
        return df

# ============================================================================
# TRADING ENGINE
# ============================================================================

class TradingBot:
    """Main trading bot engine (one instance per dashboard user / Upstox account).

    Per-user logs under src/server/data/users/<username>/logs/ (not shared across accounts):

    - trading_bot.log — Combined operational log: bot messages (API errors, signals, EOD) and
      optional market-status lines (same file, logger name in brackets). Also mirrored to stdout
      with a [username] prefix.

    - orders.log — Structured ENTRY / EXIT / SKIP / ORDER_FAILED lines only; parsed by the dashboard
      for P&L and history. Kept separate and smaller on disk.

    - paper_orders.log — PAPER_ENTRY / PAPER_EXIT for non-live symbols (no broker orders); dashboard paper P&L.

    - Set TRADING_BOT_WRITE_MARKET_STATUS_LOG=0 to skip writing market-status lines to the file
      (console-only for that stream).
    """

    def __init__(self, config, client: UpstoxClient, username: str):
        self.username = sanitize_username(username)
        self.config = config
        self.client = client
        self.state_file = user_data_dir(self.username) / "trading_state.json"
        logs_dir = user_data_dir(self.username) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        write_market_status_file = os.environ.get(
            "TRADING_BOT_WRITE_MARKET_STATUS_LOG", "1"
        ).strip().lower() not in ("0", "false", "no", "")

        fmt_console = logging.Formatter(
            f"%(asctime)s - %(levelname)s - [{self.username}] %(message)s"
        )

        self._bot_logger = logging.getLogger(f"trading_bot.{self.username}")
        self._bot_logger.setLevel(logging.INFO)
        self._bot_logger.propagate = False
        self._bot_logger.handlers.clear()
        ops_fmt = logging.Formatter(
            "%(asctime)s - %(levelname)s - [%(name)s] %(message)s"
        )
        ops_fh = logging.FileHandler(logs_dir / "trading_bot.log", encoding="utf-8")
        ops_fh.setFormatter(ops_fmt)
        self._bot_logger.addHandler(ops_fh)
        bc = logging.StreamHandler(sys.stdout)
        bc.setFormatter(fmt_console)
        self._bot_logger.addHandler(bc)

        self._order_logger = logging.getLogger(f"orders.{self.username}")
        self._order_logger.setLevel(logging.INFO)
        self._order_logger.propagate = False
        self._order_logger.handlers.clear()
        oh = logging.FileHandler(logs_dir / "orders.log", encoding="utf-8")
        oh.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
        self._order_logger.addHandler(oh)

        self._paper_logger = logging.getLogger(f"paper_orders.{self.username}")
        self._paper_logger.setLevel(logging.INFO)
        self._paper_logger.propagate = False
        self._paper_logger.handlers.clear()
        ph = logging.FileHandler(logs_dir / "paper_orders.log", encoding="utf-8")
        ph.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
        self._paper_logger.addHandler(ph)

        self._market_status_logger = logging.getLogger(f"market_status.{self.username}")
        self._market_status_logger.setLevel(logging.INFO)
        self._market_status_logger.propagate = False
        self._market_status_logger.handlers.clear()
        if write_market_status_file:
            self._market_status_logger.addHandler(ops_fh)
        else:
            self._market_status_logger.addHandler(logging.NullHandler())

        self.positions = {}
        self.paper_positions = {}
        self.option_positions = {}
        self.paper_total_pnl = 0.0
        self.total_pnl = 0
        self.running = True
        self.analyzer = TechnicalAnalyzer()
        self.entry_warmup_done = False
        self.entry_warmup_timestamps = {}
        self.last_entry_candle_processed = {}
        self.last_position_eval_logged = {}
        self.eod_squareoff_done = {}
        self.dashboard_client = DashboardClient(
            self.username,
            enabled=DASHBOARD_CONFIG.get("enabled", True),
            base_url=DASHBOARD_CONFIG.get("base_url", "http://localhost:8000"),
            timeout_seconds=float(DASHBOARD_CONFIG.get("timeout_seconds", 2.0)),
        )
        self.dashboard_batch_size = int(DASHBOARD_CONFIG.get("batch_size", 50))
        self.pending_live_updates = {}
        self.archive_requested = False
        self._last_contract_roll_attempt = {}
        self._last_index_fo_token_refresh_ts = 0.0
        self._kite_tick_stream: KiteTickStream | None = None
        self._kite_script_tokens: dict[str, int] = {}
        self._mcx_instruments_cache = []
        self._mcx_instruments_cache_at = 0.0
        self._nse_instruments_cache = []
        self._nse_instruments_cache_at = 0.0
        self._bse_instruments_cache = []
        self._bse_instruments_cache_at = 0.0
        self._cycle_scope_logged_once = False
        self._strategy_lock = threading.Lock()
        self._script_data_cache: dict[str, dict] = {}
        self._last_stream_exit_mono = 0.0
        self._stream_exit_min_interval = max(
            0.0,
            float(os.environ.get("KITE_STREAM_EXIT_MIN_INTERVAL_SEC", "0.025") or 0.0),
        )
        self.client._log = self._bot_logger
        # Fill blank NSE/BSE FO tokens (e.g., stock futures) on startup.
        self._seed_missing_fo_contract_tokens()

    def load_state(self):
        """Load saved trading state"""
        try:
            if self.state_file.exists():
                with open(self.state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                    self.positions = state.get("positions", {})
                    self.paper_positions = state.get("paper_positions", {})
                    self.option_positions = state.get("option_positions", {})
                    self.paper_total_pnl = float(state.get("paper_total_pnl", 0.0))
                    self.total_pnl = state.get("total_pnl", 0)
                    self.eod_squareoff_done = state.get("eod_squareoff_done", {})
                    for _sn, _pos in list(self.paper_positions.items()):
                        self._ensure_position_fields(_pos, _sn)
                    self._bot_logger.info(
                        f"STATE LOADED: {len(self.positions)} live + {len(self.paper_positions)} paper positions"
                    )
        except Exception as e:
            self._bot_logger.warning(f"WARNING: Could not load state: {e}")

    def save_state(self):
        """Save current trading state"""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "positions": self.positions,
                "paper_positions": self.paper_positions,
                "option_positions": self.option_positions,
                "paper_total_pnl": self.paper_total_pnl,
                "total_pnl": self.total_pnl,
                "eod_squareoff_done": self.eod_squareoff_done,
                "timestamp": datetime.now().isoformat(),
            }
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            self._bot_logger.error(f"ERROR: Could not save state: {e}")

    def _ensure_position_fields(self, position, script_name=None):
        """Backfill position fields for older saved state compatibility."""
        entry_price = position.get('entry_price', 0)
        position_type = position.get('type')
        risk_percent = self.config['trailing_stop_loss_percent'] / 100
        quantity = float(position.get('quantity', self._get_order_quantity(script_name) if script_name else 1))
        _, nse_target = self._nse_rupee_sl_target_prices(
            script_name=script_name,
            position_type=position_type,
            entry_price=entry_price,
            quantity=quantity,
        )

        if 'initial_sl' not in position:
            if position_type == 'BUY':
                position['initial_sl'] = entry_price * (1 - risk_percent)
            elif position_type == 'SELL':
                position['initial_sl'] = entry_price * (1 + risk_percent)

        if 'stop_loss' not in position:
            position['stop_loss'] = position.get('initial_sl', entry_price)

        if 'trail_steps_locked' not in position:
            position['trail_steps_locked'] = 0

        if 'breakeven_done' not in position:
            position['breakeven_done'] = False

        if 'profit_lock_r_locked' not in position:
            position['profit_lock_r_locked'] = 0.0

        if 'profit_lock_trigger_r_locked' not in position:
            position['profit_lock_trigger_r_locked'] = 0.0

        if 'max_favorable_pnl' not in position:
            position['max_favorable_pnl'] = 0.0

        if 'money_lock_steps_locked' not in position:
            position['money_lock_steps_locked'] = 0

        if 'money_lock_pnl_locked' not in position:
            position['money_lock_pnl_locked'] = 0.0

        if 'target_price' not in position and entry_price > 0:
            if nse_target is not None:
                position['target_price'] = nse_target
            else:
                target_percent = self.config['target_percent'] / 100
                if position_type == 'BUY':
                    position['target_price'] = entry_price * (1 + target_percent)
                elif position_type == 'SELL':
                    position['target_price'] = entry_price * (1 - target_percent)

        if 'win_percent' not in position:
            position['win_percent'] = None
        if 'chart_percent' not in position:
            position['chart_percent'] = None
        if 'chart_volume' not in position:
            position['chart_volume'] = None
        if 'win_percent_source' not in position:
            position['win_percent_source'] = "legacy_backfill_pending"
        if script_name and position.get('win_percent_source') in {
            "legacy_backfill_pending",
            "legacy_backfill_v1",
        }:
            position['win_percent'] = self._backfill_win_percent(script_name, position)
            position['win_percent_source'] = "legacy_backfill_v2"

        # ADX gate signal values (persist for auditability in orders.log).
        # These may be missing from older saved state.
        if 'signal_adx' not in position:
            position['signal_adx'] = float(position.get('signal_adx', 0.0) or 0.0)
        if 'signal_plus_di' not in position:
            position['signal_plus_di'] = float(position.get('signal_plus_di', 0.0) or 0.0)
        if 'signal_minus_di' not in position:
            position['signal_minus_di'] = float(position.get('signal_minus_di', 0.0) or 0.0)

        if 'entry_time' not in position:
            position['entry_time'] = datetime.now().isoformat()

        if 'quantity' not in position:
            position['quantity'] = self._get_order_quantity(script_name) if script_name else 1

        if 'last_polled_price' not in position:
            position['last_polled_price'] = None

        if 'trade_id' not in position:
            script_for_id = script_name or "UNKNOWN"
            position['trade_id'] = self._build_trade_id(script_for_id, position['entry_time'])

    def _backfill_win_percent(self, script_name, position):
        """
        Estimate win% for legacy live positions that were opened before win_percent
        started getting stored explicitly.
        """
        try:
            ema_short = float(position.get('signal_ema_short', 0.0) or 0.0)
            ema_long = float(position.get('signal_ema_long', 0.0) or 0.0)
            side = str(position.get('type', '')).upper()
            if side == 'BUY':
                ema_slope_ok = ema_short >= ema_long
            elif side == 'SELL':
                ema_slope_ok = ema_short <= ema_long
            else:
                ema_slope_ok = False

            ema_sep_pct = abs(ema_short - ema_long) / ema_long * 100 if ema_long > 0 else 0.0
            min_sep_pct = self._get_min_ema_separation_percent(script_name)
            ob_percent = float(position.get('ob_percent', 0.0) or 0.0)

            probability, _ = self._estimate_trade_probability(
                script_name=script_name,
                ema_slope_ok=ema_slope_ok,
                ema_sep_pct=ema_sep_pct,
                min_sep_pct=min_sep_pct,
                ob_percent=ob_percent,
                level_metrics=None,
            )
            return float(probability)
        except Exception:
            return None

    def _backfill_chart_percent(self, script_name, position, signal_df):
        """
        Backfill chart_percent for legacy open positions using stored signal_time
        and current signal dataframe.
        """
        try:
            side = str(position.get('type', '')).upper()
            if side not in {"BUY", "SELL"}:
                return None

            signal_time_raw = position.get('signal_time')
            if not signal_time_raw:
                return None

            signal_ts = pd.to_datetime(signal_time_raw, errors='coerce')
            if pd.isna(signal_ts):
                return None

            chart_pct, chart_vol = self._compute_chart_ob_snapshot(signal_df, signal_ts, side)
            if chart_vol is not None:
                position['chart_volume'] = chart_vol
            return chart_pct
        except Exception:
            return None

    @staticmethod
    def _build_trade_id(script_name, opened_at):
        return f"{script_name}-{opened_at}"

    @staticmethod
    def _calculate_realized_pnl(side, entry_price, exit_price, quantity):
        if side == "BUY":
            return (exit_price - entry_price) * quantity
        return (entry_price - exit_price) * quantity

    def _build_dashboard_trade_payload(self, script_name, position, last_price=None, exit_price=None, closed_at=None):
        self._ensure_position_fields(position, script_name)

        side = position.get("type", "BUY")
        quantity = float(position.get("quantity", self._get_order_quantity(script_name)))
        entry_price = float(position.get("entry_price", 0.0))
        opened_at = position.get("entry_time", datetime.now().isoformat())
        trade_id = position.get("trade_id", self._build_trade_id(script_name, opened_at))

        current_price = float(entry_price if last_price is None else last_price)
        payload = {
            "id": trade_id,
            "symbol": script_name,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "stop_loss": float(position.get("stop_loss", entry_price)),
            "target_price": float(position.get("target_price", entry_price)),
            "chart_percent": (
                float(position.get("chart_percent"))
                if position.get("chart_percent") is not None
                else None
            ),
            "chart_volume": (
                float(position.get("chart_volume"))
                if position.get("chart_volume") is not None
                else None
            ),
            "win_percent": (
                float(position.get("win_percent"))
                if position.get("win_percent") is not None
                else None
            ),
            "exit_price": None,
            "last_price": current_price,
            "unrealized_pnl": self._calculate_realized_pnl(side, entry_price, current_price, quantity),
            "realized_pnl": None,
            "opened_at": opened_at,
            "closed_at": None,
            "manual_execution": bool(position.get("manual_execution")),
        }

        if exit_price is not None:
            final_exit = float(exit_price)
            final_closed_at = closed_at or datetime.now().isoformat()
            payload["exit_price"] = final_exit
            payload["last_price"] = final_exit
            payload["unrealized_pnl"] = None
            payload["realized_pnl"] = self._calculate_realized_pnl(side, entry_price, final_exit, quantity)
            payload["closed_at"] = final_closed_at

        return payload

    def _notify_dashboard_trade_open(self, script_name, position, last_price):
        payload = self._build_dashboard_trade_payload(script_name, position, last_price=last_price)
        self.dashboard_client.post_trade_open(payload)

    def _queue_dashboard_trade_update(self, script_name, position, last_price):
        payload = self._build_dashboard_trade_payload(script_name, position, last_price=last_price)
        self.pending_live_updates[payload["id"]] = payload

    def _flush_dashboard_trade_updates(self):
        if not self.pending_live_updates:
            return

        trades = list(self.pending_live_updates.values())
        chunk_size = max(1, self.dashboard_batch_size)
        all_ok = True
        for start in range(0, len(trades), chunk_size):
            chunk = trades[start:start + chunk_size]
            ok = self.dashboard_client.post_trade_update_batch(chunk)
            if not ok:
                all_ok = False
                break

        if all_ok:
            self.pending_live_updates.clear()

    def _notify_dashboard_trade_close(self, script_name, position, exit_price):
        payload = self._build_dashboard_trade_payload(
            script_name,
            position,
            last_price=exit_price,
            exit_price=exit_price,
            closed_at=datetime.now().isoformat(),
        )
        self.dashboard_client.post_trade_close(payload)

    def _log_order_event(self, script_name, action, side, price, reason, extra=""):
        self._order_logger.info(
            f"{script_name} | ACTION={action} | SIDE={side} | PRICE={price:.2f} | REASON={reason}"
            + (f" | {extra}" if extra else "")
        )

    def _log_skip_event(self, script_name, side, price, reason, extra=""):
        if is_paper_script(script_name):
            self._bot_logger.info(
                f"PAPER SKIP: {script_name} {side} @ Rs{price:.2f} | {reason} | {extra}"
            )
            return
        self._log_order_event(
            script_name=script_name,
            action="SKIP",
            side=side,
            price=price,
            reason=reason,
            extra=extra,
        )

    def _log_order_failure(self, script_name, side, price, reason, error_text, endpoint=""):
        fail_extra = f"error={error_text}"
        if endpoint:
            fail_extra += f"; endpoint={endpoint}"
        self._order_logger.info(
            f"{script_name} | ACTION=ORDER_FAILED | SIDE={side} | PRICE={price:.2f} | REASON={reason} | {fail_extra}"
        )

    def _log_paper_order_event(self, script_name, action, side, price, reason, extra=""):
        qty = self._get_order_quantity(script_name)
        self._paper_logger.info(
            f"{script_name} | ACTION={action} | SIDE={side} | PRICE={price:.2f} | REASON={reason} | qty={qty}"
            + (f" | {extra}" if extra else "")
        )

    def _paper_exit_after_signal(
        self,
        script_name,
        position,
        exit_side,
        current_price,
        reason,
        extra_log="",
    ):
        qty = float(position.get("quantity", self._get_order_quantity(script_name)))
        realized = self._calculate_realized_pnl(
            position["type"],
            float(position["entry_price"]),
            float(current_price),
            qty,
        )
        self.paper_total_pnl += realized
        self._log_paper_order_event(
            script_name,
            "PAPER_EXIT",
            exit_side,
            current_price,
            reason,
            extra=(
                f"entry={position['entry_price']:.2f}; realized_pnl={realized:.2f}; qty={qty}; {extra_log}".strip()
            ),
        )
        if telegram_notifications_enabled_for_user(self.username):
            if not send_paper_trade_notification(
                {
                    "account": self.username,
                    "symbol": script_name,
                    "action": exit_side,
                    "quantity": qty,
                    "price": current_price,
                    "reason": reason,
                    "realized_pnl": realized,
                    "stop_loss": position.get("stop_loss"),
                    "target_price": position.get("target_price"),
                    "entry_adx": float(position.get("signal_adx", 0.0) or 0.0),
                    "entry_plus_di": float(position.get("signal_plus_di", 0.0) or 0.0),
                    "entry_minus_di": float(position.get("signal_minus_di", 0.0) or 0.0),
                    "timestamp": self._now_ist(),
                },
                is_entry=False,
            ):
                self._bot_logger.error(
                    f"Failed Telegram PAPER EXIT: {script_name} {exit_side} qty={qty} @ Rs{current_price:.2f}"
                )
        del self.paper_positions[script_name]
        self.save_state()

    def _place_order_with_result(
        self,
        script_name,
        side,
        price,
        reason,
        stop_loss=None,
        target_price=None,
        win_percent=None,
        chart_percent=None,
        chart_volume=None,
        realized_pnl=None,
        entry_adx=None,
        entry_plus_di=None,
        entry_minus_di=None,
    ):
        order_token = self._get_order_token(script_name)
        order_qty = self._get_order_quantity(script_name)
        result = self.client.place_order(order_token, order_qty, side)
        if result and result.get('status') == 'success':
            trade = {
                "account": self.username,
                "symbol": script_name,
                "action": side,
                "quantity": order_qty,
                "price": price,
                "reason": reason,
                "stop_loss": stop_loss,
                "target_price": target_price,
                "win_percent": win_percent,
                "chart_percent": chart_percent,
                "chart_volume": chart_volume,
                "realized_pnl": realized_pnl,
                "entry_adx": entry_adx,
                "entry_plus_di": entry_plus_di,
                "entry_minus_di": entry_minus_di,
                "timestamp": self._now_ist(),
            }
            if telegram_notifications_enabled_for_user(self.username):
                if not send_trade_notification(trade):
                    self._bot_logger.error(
                        f"Failed to send Telegram notification for trade: "
                        f"{script_name} {side} qty={order_qty} @ Rs{price:.2f}"
                    )
            return True, result

        error_text = (result or {}).get('error', 'Unknown error')
        endpoint = (result or {}).get('endpoint', '')
        self._bot_logger.error(
            f"ORDER FAILED: {script_name} {side} qty={order_qty} @ Rs{price:.2f} | reason={reason} | error={error_text}"
        )
        self._log_order_failure(script_name, side, price, reason, error_text, endpoint)
        if telegram_notifications_enabled_for_user(self.username):
            alert = {
                "account": self.username,
                "symbol": script_name,
                "action": side,
                "quantity": order_qty,
                "price": price,
                "reason": "ORDER_FAILED",
                "stop_loss": stop_loss,
                "target_price": target_price,
                "entry_adx": entry_adx,
                "entry_plus_di": entry_plus_di,
                "entry_minus_di": entry_minus_di,
                "error_text": error_text,
                "endpoint": endpoint,
                "note": "Place manually in Upstox app/web",
                "timestamp": self._now_ist(),
            }
            if not send_trade_notification(alert):
                self._bot_logger.error(
                    f"Failed Telegram ORDER_FAILED alert: {script_name} {side} qty={order_qty} @ Rs{price:.2f}"
                )
        return False, result

    @staticmethod
    def _mcx_api_disabled_error(error_text: str) -> bool:
        t = str(error_text or "").strip().lower()
        return "mcx orders via api are temporarily disabled" in t

    def _is_mcx_manual_track_candidate(self, script_name: str, order_result: dict | None) -> bool:
        token = str(self._get_order_token(script_name) or "").strip()
        if not token.startswith("MCX_FO|"):
            return False
        error_text = str((order_result or {}).get("error", "")).strip()
        return self._mcx_api_disabled_error(error_text)

    def _notify_manual_close_needed(self, script_name, position, exit_side, current_price, reason):
        qty = float(position.get("quantity", self._get_order_quantity(script_name)))
        trade = {
            "account": self.username,
            "symbol": script_name,
            "action": exit_side,
            "quantity": qty,
            "price": current_price,
            "reason": "ORDER_FAILED",
            "stop_loss": position.get("stop_loss"),
            "target_price": position.get("target_price"),
            "entry_adx": float(position.get("signal_adx", 0.0) or 0.0),
            "entry_plus_di": float(position.get("signal_plus_di", 0.0) or 0.0),
            "entry_minus_di": float(position.get("signal_minus_di", 0.0) or 0.0),
            "error_text": f"Manual close signal: {reason}",
            "note": "Close manually in Upstox app/web",
            "timestamp": self._now_ist(),
        }
        if telegram_notifications_enabled_for_user(self.username):
            ok = send_trade_notification(trade)
            if not ok:
                self._bot_logger.error(
                    f"Failed Telegram manual-close alert: {script_name} {exit_side} @ Rs{current_price:.2f}"
                )
        self._order_logger.info(
            f"{script_name} | ACTION=MANUAL_CLOSE_NEEDED | SIDE={exit_side} | PRICE={current_price:.2f} "
            f"| REASON={reason} | entry={float(position.get('entry_price', 0.0)):.2f}; "
            f"sl={float(position.get('stop_loss', 0.0)):.2f}; target={float(position.get('target_price', 0.0)):.2f}"
        )

    def _get_order_token(self, script_name):
        """Get the order token for placing orders (FUTURES/COMMODITIES)"""
        order_tokens = self.config.get('order_tokens', {})
        return order_tokens.get(script_name, self.config['scripts'].get(script_name, ''))

    def _lot_size_for_instrument_key(self, instrument_key: str) -> int:
        if not isinstance(instrument_key, str) or not instrument_key:
            return 0
        rows = []
        try:
            if instrument_key.startswith("NSE_FO|"):
                rows = self._fetch_exchange_instruments(NSE_INSTRUMENTS_URL, "nse")
            elif instrument_key.startswith("BSE_FO|"):
                rows = self._fetch_exchange_instruments(BSE_INSTRUMENTS_URL, "bse")
            elif instrument_key.startswith("MCX_FO|"):
                rows = self._fetch_mcx_instruments()
        except Exception:
            return 0
        for row in rows:
            if str(row.get("instrument_key", "")) != instrument_key:
                continue
            try:
                return int(float(row.get("lot_size", 0) or 0))
            except Exception:
                return 0
        return 0

    def _seed_missing_fo_contract_tokens(self):
        scripts = self.config.get("scripts", {}) or {}
        if not isinstance(scripts, dict):
            return
        for script_name, token in list(scripts.items()):
            tok = str(token or "").strip()
            if tok.startswith(("NSE_FO|", "BSE_FO|", "MCX_FO|")):
                continue
            candidates = self._get_fo_contract_candidates(script_name)
            if not candidates:
                continue
            selected = (
                self._select_index_fo_contract_avoiding_expiring_front(script_name)
                if script_name in _INDEX_FO_SCRIPT_NAMES
                else candidates[0]
            )
            self.config.setdefault("scripts", {})[script_name] = selected
            self.config.setdefault("order_tokens", {})[script_name] = selected
            cfg_lot = int(self.config.get("lot_sizes", {}).get(script_name, 0) or 0)
            if cfg_lot <= 0:
                lot = self._lot_size_for_instrument_key(selected)
                if lot > 0:
                    self.config.setdefault("lot_sizes", {})[script_name] = lot
            self._bot_logger.info(f"INIT CONTRACT: {script_name} -> {selected}")

    def _get_order_quantity(self, script_name):
        """Get exchange quantity as lots multiplied by contract lot size."""
        overrides = self.config.get("order_quantity_override_by_script", {}) or {}
        if script_name in overrides:
            try:
                return max(1, int(float(overrides.get(script_name))))
            except Exception:
                pass
        lots = int(self.config.get('quantity', 1))
        lot_size = int(self.config.get('lot_sizes', {}).get(script_name, 0) or 0)
        if lot_size <= 0:
            lot_size = self._lot_size_for_instrument_key(self._get_order_token(script_name))
            if lot_size <= 0:
                lot_size = 1
        return max(1, lots * lot_size)

    @staticmethod
    def _option_type_for_futures_side(futures_side: str) -> str:
        return "CE" if str(futures_side).upper() == "BUY" else "PE"

    @staticmethod
    def _option_side_for_futures_side(futures_side: str) -> str:
        # Long CE for BUY futures signal, long PE for SELL futures signal.
        return "BUY"

    @staticmethod
    def _safe_float(v, default=0.0) -> float:
        try:
            return float(v)
        except Exception:
            return float(default)

    def _options_expiry_cutoff_dt(self, now_ist: datetime) -> datetime | None:
        txt = str(self.config.get("options_expiry_day_cutoff_ist") or "12:00").strip()
        if ":" not in txt:
            return None
        hour_text, minute_text = txt.split(":", 1)
        return now_ist.replace(
            hour=int(hour_text),
            minute=int(minute_text),
            second=0,
            microsecond=0,
        )

    def _filter_option_chain_to_policy_expiry(
        self, script: str, cands: list[dict]
    ) -> tuple[list[dict], int]:
        """
        NIFTY/SENSEX: nearest calendar expiry (front weekly in practice).
        BANKNIFTY: nearest monthly expiry (last Thursday of month in IST).
        Returns (filtered rows, chosen expiry_ms).
        """
        if not cands:
            return [], 0
        positive = [c for c in cands if int(c.get("expiry_ms") or 0) > 0]
        if not positive:
            return [], 0
        if script == "BANKNIFTY":
            monthly = [c for c in positive if _is_last_thursday_of_month_ist(int(c["expiry_ms"]))]
            if monthly:
                target_ms = min(int(c["expiry_ms"]) for c in monthly)
                filt = [c for c in monthly if int(c["expiry_ms"]) == target_ms]
                return filt, target_ms
            self._bot_logger.warning(
                "OPTIONS: BANKNIFTY — no last-Thursday monthly expiry in chain; using nearest expiry"
            )
            target_ms = min(int(c["expiry_ms"]) for c in positive)
            return [c for c in positive if int(c["expiry_ms"]) == target_ms], target_ms
        target_ms = min(int(c["expiry_ms"]) for c in positive)
        return [c for c in positive if int(c["expiry_ms"]) == target_ms], target_ms

    def _resolve_atm_option_for_script(self, script_name: str, underlying_price: float, option_type: str):
        """
        Pick ATM option from FO instrument master using expiry policy:
        NIFTY/SENSEX → nearest expiry (front weeklies); BANKNIFTY → nearest monthly (last Thursday).
        """
        script = str(script_name or "").upper().strip()
        if script not in {"NIFTY", "BANKNIFTY", "SENSEX"}:
            return None
        exchange = "bse" if script == "SENSEX" else "nse"
        seg = "BSE_FO" if script == "SENSEX" else "NSE_FO"
        url = BSE_INSTRUMENTS_URL if exchange == "bse" else NSE_INSTRUMENTS_URL
        try:
            instruments = self._fetch_exchange_instruments(url, exchange)
        except Exception as e:
            self._bot_logger.warning(f"Unable to fetch option chain for {script}: {e}")
            return None
        now_ms = int(time.time() * 1000)
        opt_type = str(option_type or "").upper().strip()
        cands = []
        for row in instruments:
            ikey = str(row.get("instrument_key", "")).strip()
            if not ikey.startswith(f"{seg}|"):
                continue
            ins_type = str(row.get("instrument_type", "")).upper().strip()
            if ins_type not in {"CE", "PE"}:
                continue
            if ins_type != opt_type:
                continue
            tsym = str(row.get("trading_symbol", "")).upper().strip()
            if not tsym.startswith(script):
                continue
            expiry_ms = int(float(row.get("expiry", 0) or 0))
            if expiry_ms and expiry_ms < now_ms:
                continue
            strike = self._safe_float(
                row.get("strike_price", row.get("strike", row.get("strikePrice", 0.0))),
                0.0,
            )
            if strike <= 0:
                continue
            lot_size = int(self._safe_float(row.get("lot_size", 0), 0))
            cands.append(
                {
                    "instrument_key": ikey,
                    "trading_symbol": tsym,
                    "expiry_ms": expiry_ms,
                    "strike": strike,
                    "lot_size": max(1, lot_size),
                }
            )
        if not cands:
            return None
        cands, target_ms = self._filter_option_chain_to_policy_expiry(script, cands)
        if not cands:
            return None
        self._bot_logger.info(
            "OPTIONS CHAIN: %s %s expiry_ms=%s policy=%s",
            script,
            opt_type,
            target_ms,
            "MONTHLY" if script == "BANKNIFTY" else "NEAREST_WEEKLY",
        )
        cands.sort(key=lambda c: (abs(c["strike"] - underlying_price), c["strike"]))
        return cands[0]

    def _option_hybrid_sl_premium(
        self,
        entry_underlying: float,
        sl_underlying: float,
        entry_option: float,
    ) -> float:
        """
        Long-option protective sell: map futures-defined risk to premium using an assumed ATM delta,
        with a minimum premium floor (no live Greeks API).
        """
        delta = float(self.config.get("options_sl_delta_assumption", 0.5))
        floor_ratio = float(self.config.get("options_sl_min_premium_floor_ratio", 0.35))
        risk = abs(float(entry_underlying) - float(sl_underlying))
        raw_sl = float(entry_option) - delta * risk
        floor_sl = max(0.0, float(entry_option) * floor_ratio)
        sl = max(floor_sl, raw_sl)
        if sl >= float(entry_option):
            sl = max(0.0, float(entry_option) * 0.99)
        return sl

    def _options_scaled_rupee_profit_target(self, opt: dict) -> float | None:
        """Scale futures nse_trade_pnl_levels.target_pnl by option lots vs reference futures lots."""
        ob = self.config.get("options_rupee_profit_booking") or {}
        if not bool(ob.get("enabled", True)):
            return None
        nse = self.config.get("nse_trade_pnl_levels") or {}
        if not bool(nse.get("enabled", True)):
            return None
        base = float(nse.get("target_pnl", 5000.0))
        if base <= 0:
            return None
        ref_raw = ob.get("futures_lots_reference")
        if ref_raw is None:
            ref_lots = max(1, int(self.config.get("quantity", 1)))
        else:
            ref_lots = max(1, int(ref_raw))
        opt_lots = max(1, int(opt.get("total_lots", self.config.get("options_total_lots", 4))))
        return base * (opt_lots / float(ref_lots))

    @staticmethod
    def _round_to_tick(p: float, tick: float = 0.05) -> float:
        return round(round(float(p) / tick) * tick, 2)

    def _compute_option_premium_r(
        self,
        fut_entry: float,
        fut_sl: float,
        strike: float,
        expiry_ms: int,
        opt_type: str,
        opt_entry: float,
    ) -> float:
        """
        Premium 'R' for 1:1 / 1:2 / 1:3 GTT spacing: ~|delta| * futures risk (BS delta if enabled).
        """
        fut_risk = abs(float(fut_entry) - float(fut_sl))
        if fut_risk <= 0:
            fut_risk = max(1.0, abs(float(fut_entry)) * 0.002)
        use_bs = bool(self.config.get("options_use_bs_delta_for_r", True))
        if use_bs:
            T = years_to_expiry_from_ms(int(expiry_ms))
            sigma = float(self.config.get("options_iv_annual", 0.18))
            rfr = float(self.config.get("options_risk_free_rate", 0.0))
            S = float(fut_entry)
            K = float(strike) if float(strike) > 0 else float(fut_entry)
            if str(opt_type).upper() == "CE":
                d = bs_call_delta(S, K, T, sigma, rfr)
            else:
                d = bs_put_delta(S, K, T, sigma, rfr)
            delta = abs(float(d))
        else:
            delta = float(self.config.get("options_sl_delta_assumption", 0.5))
        r_prem = delta * fut_risk
        return max(r_prem, 1.0)

    def _cancel_all_option_gtts(self, opt_pos: dict) -> None:
        if opt_pos.get("gtt_ladder"):
            gids = opt_pos.get("gtt_ids") or {}
            for name in ("sl", "tp1", "tp2", "tp3"):
                gid = str(gids.get(name) or "").strip()
                if not gid:
                    continue
                res = self.client.cancel_gtt_order(gid)
                if res.get("status") == "success":
                    self._bot_logger.info(f"OPTIONS GTT cancel {name}: id={gid}")
                else:
                    self._bot_logger.warning(
                        f"OPTIONS GTT cancel failed {name}: id={gid} err={res.get('error')}"
                    )
            opt_pos["gtt_ids"] = {"sl": "", "tp1": "", "tp2": "", "tp3": ""}
            return
        gtt_id = str(opt_pos.get("active_sl_gtt_id") or "").strip()
        if not gtt_id:
            return
        res = self.client.cancel_gtt_order(gtt_id)
        if res.get("status") == "success":
            self._bot_logger.info(f"OPTIONS GTT cancel success: gtt_id={gtt_id}")
        else:
            self._bot_logger.warning(
                f"OPTIONS GTT cancel failed: gtt_id={gtt_id} err={res.get('error')}"
            )
        opt_pos["active_sl_gtt_id"] = ""

    def _cancel_option_gtt_if_any(self, opt_pos: dict) -> None:
        """Legacy single SL GTT cancel."""
        if opt_pos.get("gtt_ladder"):
            return
        gtt_id = str(opt_pos.get("active_sl_gtt_id") or "").strip()
        if not gtt_id:
            return
        res = self.client.cancel_gtt_order(gtt_id)
        if res.get("status") == "success":
            self._bot_logger.info(f"OPTIONS GTT cancel success: gtt_id={gtt_id}")
        else:
            self._bot_logger.warning(
                f"OPTIONS GTT cancel failed: gtt_id={gtt_id} err={res.get('error')}"
            )
        opt_pos["active_sl_gtt_id"] = ""

    def _place_or_replace_option_sl_gtt(self, script_name: str, opt_pos: dict) -> None:
        if not bool(self.config.get("options_gtt_enabled", True)):
            return
        self._cancel_option_gtt_if_any(opt_pos)
        qty_lots = int(opt_pos.get("remaining_lots", 0))
        lot_size = int(opt_pos.get("lot_size", 1))
        qty = max(0, qty_lots * lot_size)
        if qty <= 0:
            return
        trigger = self._safe_float(opt_pos.get("sl_option_price", 0.0), 0.0)
        if trigger <= 0:
            return
        res = self.client.place_gtt_order(
            instrument_key=str(opt_pos.get("instrument_key") or ""),
            quantity=qty,
            transaction_type="SELL",
            trigger_price=trigger,
            limit_price=trigger,
            tag=f"opt_sl_{script_name.lower()}",
        )
        if res.get("status") == "success":
            gtt_data = res.get("data") or {}
            opt_pos["active_sl_gtt_id"] = str(
                gtt_data.get("id", gtt_data.get("gtt_id", gtt_data.get("trigger_id", "")))
            )
            self._bot_logger.info(
                f"OPTIONS GTT placed: {script_name} trigger={trigger:.2f} qty={qty} id={opt_pos.get('active_sl_gtt_id')}"
            )
        else:
            self._bot_logger.warning(
                f"OPTIONS GTT placement failed for {script_name}: {res.get('error')}"
            )

    @staticmethod
    def _gtt_id_from_response(res: dict) -> str:
        d = res.get("data") or {}
        return str(d.get("id", d.get("gtt_id", d.get("trigger_id", ""))))

    def _place_option_gtt_ladder(self, script_name: str, opt: dict) -> None:
        """Place SL + three TP GTTs on option premium (exchange may cap concurrent GTTs per symbol)."""
        if not bool(self.config.get("options_gtt_enabled", True)):
            return
        ikey = str(opt.get("instrument_key") or "")
        lot_size = max(1, int(opt.get("lot_size", 1)))
        total_lots = max(1, int(opt.get("total_lots", 4)))
        splits = list(self.config.get("options_tp_lot_splits") or [2, 1, 1])
        if len(splits) != 3 or sum(splits) != total_lots:
            self._bot_logger.error(
                "OPTIONS: options_tp_lot_splits must be 3 integers summing to options_total_lots; using [2,1,1]"
            )
            splits = [2, 1, 1]
        entry_o = float(opt.get("entry_price_option") or 0.0)
        R = float(opt.get("premium_r") or 0.0)
        if entry_o <= 0 or R <= 0:
            self._bot_logger.warning("OPTIONS LADDER: missing entry or premium R; skipping GTT ladder")
            return
        sl_trig = self._round_to_tick(entry_o - R)
        tp1_trig = self._round_to_tick(entry_o + R)
        tp2_trig = self._round_to_tick(entry_o + 2.0 * R)
        tp3_trig = self._round_to_tick(entry_o + 3.0 * R)
        qty_sl = total_lots * lot_size
        q1, q2, q3 = [max(1, int(s) * lot_size) for s in splits]
        gtt_ids: dict[str, str] = {"sl": "", "tp1": "", "tp2": "", "tp3": ""}

        def _place(bucket: str, tag: str, qty: int, trig: float) -> None:
            if qty <= 0 or trig <= 0:
                return
            res = self.client.place_gtt_order(
                instrument_key=ikey,
                quantity=int(qty),
                transaction_type="SELL",
                trigger_price=float(trig),
                limit_price=float(trig),
                tag=tag,
            )
            if res.get("status") == "success":
                gtt_ids[bucket] = self._gtt_id_from_response(res)
                self._bot_logger.info(
                    "OPTIONS LADDER GTT: %s %s qty=%d trigger=%.2f id=%s",
                    script_name,
                    tag,
                    qty,
                    trig,
                    gtt_ids[bucket],
                )
            else:
                self._bot_logger.error(
                    "OPTIONS LADDER GTT FAILED: %s %s err=%s",
                    script_name,
                    tag,
                    res.get("error"),
                )

        _place("sl", f"opt_sl_{script_name.lower()}", qty_sl, sl_trig)
        _place("tp1", f"opt_tp1_{script_name.lower()}", q1, tp1_trig)
        _place("tp2", f"opt_tp2_{script_name.lower()}", q2, tp2_trig)
        _place("tp3", f"opt_tp3_{script_name.lower()}", q3, tp3_trig)
        opt["gtt_ids"] = gtt_ids
        opt["sl_option_price"] = float(sl_trig)
        opt["tp_triggers"] = {"tp1": tp1_trig, "tp2": tp2_trig, "tp3": tp3_trig}

    def _net_option_units_for_instrument(self, instrument_key: str) -> int | None:
        for row in self.client.get_short_term_positions():
            key = str(row.get("instrument_token") or row.get("instrument_key") or "")
            if key != instrument_key:
                continue
            for fld in ("net_quantity", "quantity", "day_net_quantity"):
                v = row.get(fld)
                if v is None:
                    continue
                try:
                    return int(float(v))
                except (TypeError, ValueError):
                    continue
        return None

    def _refresh_ladder_sl_gtt_from_futures(self, script_name: str, opt: dict) -> None:
        """Cancel/replace SL GTT when futures trailing stop tightens (hybrid premium mapping)."""
        if not bool(self.config.get("options_gtt_enabled", True)):
            return
        pos = self.positions.get(script_name)
        if not isinstance(pos, dict):
            return
        opt["sl_underlying"] = float(pos.get("stop_loss", opt.get("sl_underlying", 0.0)))
        entry_o = self._safe_float(opt.get("entry_price_option"), 0.0)
        entry_u = self._safe_float(opt.get("entry_price_underlying"), 0.0)
        sl_u = self._safe_float(opt.get("sl_underlying"), 0.0)
        if entry_o <= 0 or entry_u <= 0:
            return
        new_trig = self._round_to_tick(self._option_hybrid_sl_premium(entry_u, sl_u, entry_o))
        prev = self._safe_float(opt.get("sl_option_price"), 0.0)
        if new_trig <= prev + 0.04:
            return
        lot_size = max(1, int(opt.get("lot_size", 1)))
        rem_lots = max(0, int(opt.get("remaining_lots", 0)))
        qty = rem_lots * lot_size
        net_u = self._net_option_units_for_instrument(str(opt.get("instrument_key") or ""))
        if net_u is not None and net_u > 0:
            qty = max(1, int(net_u))
        if qty <= 0:
            return
        opt["sl_option_price"] = float(new_trig)
        gids = opt.setdefault("gtt_ids", {"sl": "", "tp1": "", "tp2": "", "tp3": ""})
        old = str(gids.get("sl") or "").strip()
        if old:
            res_c = self.client.cancel_gtt_order(old)
            if res_c.get("status") != "success":
                self._bot_logger.warning(
                    "OPTIONS LADDER: SL cancel failed (will attempt new GTT): %s", res_c.get("error")
                )
            gids["sl"] = ""
        res = self.client.place_gtt_order(
            instrument_key=str(opt.get("instrument_key") or ""),
            quantity=int(qty),
            transaction_type="SELL",
            trigger_price=float(new_trig),
            limit_price=float(new_trig),
            tag=f"opt_sl_trail_{script_name.lower()}",
        )
        if res.get("status") == "success":
            gids["sl"] = self._gtt_id_from_response(res)
            self._bot_logger.info(
                "OPTIONS LADDER SL REPLACE: %s trigger=%.2f qty=%s id=%s",
                script_name,
                new_trig,
                qty,
                gids["sl"],
            )
        else:
            self._bot_logger.warning(
                "OPTIONS LADDER SL REPLACE failed: %s err=%s", script_name, res.get("error")
            )

    def _ladder_replace_sl_breakeven(
        self, script_name: str, opt: dict, lots_open: int, reason: str
    ) -> None:
        """After TP fills, cancel/replace protective SL GTT at ~entry premium for remaining lots."""
        if not bool(self.config.get("options_gtt_enabled", True)):
            return
        lot_size = max(1, int(opt.get("lot_size", 1)))
        qty = max(1, int(lots_open) * lot_size)
        entry_o = self._safe_float(opt.get("entry_price_option"), 0.0)
        if entry_o <= 0:
            return
        trig = self._round_to_tick(entry_o * 0.999)
        gids = opt.setdefault("gtt_ids", {"sl": "", "tp1": "", "tp2": "", "tp3": ""})
        old = str(gids.get("sl") or "").strip()
        if old:
            self.client.cancel_gtt_order(old)
            gids["sl"] = ""
        res = self.client.place_gtt_order(
            instrument_key=str(opt.get("instrument_key") or ""),
            quantity=int(qty),
            transaction_type="SELL",
            trigger_price=float(trig),
            limit_price=float(trig),
            tag=f"opt_sl_be_{script_name.lower()}",
        )
        if res.get("status") == "success":
            gids["sl"] = self._gtt_id_from_response(res)
            opt["sl_option_price"] = float(trig)
            self._bot_logger.info(
                "OPTIONS LADDER BE SL: %s reason=%s qty=%d trigger=%.2f id=%s",
                script_name,
                reason,
                qty,
                trig,
                gids["sl"],
            )

    def _reconcile_ladder_partial_fills(self, script_name: str, opt: dict) -> None:
        """Use broker net qty to detect TP leg fills and refresh SL GTT (post 1:1 / 1:2)."""
        if not opt.get("gtt_ladder"):
            return
        ikey = str(opt.get("instrument_key") or "")
        lot_size = max(1, int(opt.get("lot_size", 1)))
        total_lots = max(1, int(opt.get("total_lots", 4)))
        splits = list(self.config.get("options_tp_lot_splits") or [2, 1, 1])
        u = self._net_option_units_for_instrument(ikey)
        if u is None:
            return
        opt["last_net_units"] = int(u)
        lots_open = max(0, int(u // lot_size))
        prev = max(0, int(opt.get("remaining_lots", total_lots)))
        if lots_open >= prev:
            return
        opt["remaining_lots"] = lots_open
        self._bot_logger.info(
            "OPTIONS LADDER FILL SYNC: %s net_units=%s lots_open=%s (was %s)",
            script_name,
            u,
            lots_open,
            prev,
        )
        if sum(splits) != total_lots or len(splits) != 3:
            return
        if (
            not bool(opt.get("ladder_tp1_done"))
            and lots_open <= total_lots - int(splits[0])
        ):
            opt["ladder_tp1_done"] = True
            self._ladder_replace_sl_breakeven(script_name, opt, lots_open, "POST_TP1")
        elif (
            bool(opt.get("ladder_tp1_done"))
            and not bool(opt.get("ladder_tp2_done"))
            and lots_open <= total_lots - int(splits[0]) - int(splits[1])
        ):
            opt["ladder_tp2_done"] = True
            self._ladder_replace_sl_breakeven(script_name, opt, lots_open, "POST_TP2")

    def _option_chart_crossover_should_exit(self, opt: dict) -> bool:
        """EMA crossover on option instrument intraday series (exit long option on bearish cross)."""
        if not bool(self.config.get("options_chart_crossover_exit", True)):
            return False
        interval = str(self.config.get("options_crossover_interval") or "5minute")
        ikey = str(opt.get("instrument_key") or "")
        df = self.client.get_intraday_candles(ikey, interval)
        if df is None or len(df) < 22:
            return False
        sig_df = self.analyzer.calculate_signals(df)
        if sig_df is None or len(sig_df) < 3:
            return False
        row = sig_df.iloc[-1]
        prev_sig = int(sig_df.iloc[-2].get("signal", 0))
        cur_sig = int(row.get("signal", 0))
        crossed = bool(row.get("crossover", False))
        fut_side = str(opt.get("futures_side") or "BUY").upper()
        if fut_side == "BUY":
            return crossed and prev_sig == 1 and cur_sig == -1
        return crossed and prev_sig == -1 and cur_sig == 1

    def _start_options_companion(self, script_name: str, futures_position: dict, entry_price: float, initial_sl: float):
        """Open ATM option companion trade with staged lot-management metadata."""
        if not bool(self.config.get("options_enabled", False)):
            return
        if self._script_segment(script_name) != "NSE":
            return
        if script_name not in set(self.config.get("options_scripts", [])):
            return
        if script_name in self.option_positions:
            return
        fut_side = str(futures_position.get("type") or "").upper()
        if fut_side not in {"BUY", "SELL"}:
            return
        opt_type = self._option_type_for_futures_side(fut_side)
        opt_side = self._option_side_for_futures_side(fut_side)
        atm = self._resolve_atm_option_for_script(script_name, float(entry_price), opt_type)
        if not atm:
            self._bot_logger.warning(f"OPTIONS: No ATM {opt_type} contract found for {script_name}")
            return
        now_ist = self._now_ist()
        exp_d = _ist_date_from_expiry_ms(int(atm["expiry_ms"]))
        cutoff = self._options_expiry_cutoff_dt(now_ist)
        if exp_d == now_ist.date() and cutoff is not None and now_ist >= cutoff:
            self._bot_logger.info(
                "OPTIONS SKIP: %s no new companion after %s IST on option expiry day (expiry_date=%s)",
                script_name,
                str(self.config.get("options_expiry_day_cutoff_ist") or "12:00"),
                exp_d.isoformat(),
            )
            return
        total_lots = int(self.config.get("options_total_lots", 4))
        lot_size = int(atm.get("lot_size", 1))
        qty = max(1, total_lots * lot_size)
        order = self.client.place_order(atm["instrument_key"], qty, opt_side)
        if not order or order.get("status") != "success":
            self._bot_logger.error(
                f"OPTIONS ENTRY FAILED: {script_name} {opt_type} qty={qty} err={(order or {}).get('error')}"
            )
            return
        order_id = str((order.get("data") or {}).get("order_id", "") or "").strip()
        time.sleep(0.35)
        fill_px = self.client.get_order_average_price(order_id) if order_id else None
        opt_entry_price = float(fill_px) if fill_px and float(fill_px) > 0 else 0.0
        if opt_entry_price <= 0:
            ltp = self.client.get_ltp(atm["instrument_key"])
            opt_entry_price = float(ltp) if ltp is not None and ltp > 0 else 0.0
        if order_id:
            self._bot_logger.info(
                "OPTIONS ENTRY FILL: %s order_id=%s avg_fill=%s entry_opt_used=%.2f",
                script_name,
                order_id,
                fill_px,
                float(opt_entry_price),
            )
        risk_pts = abs(float(entry_price) - float(initial_sl))
        if risk_pts <= 0:
            risk_pts = max(1.0, abs(float(entry_price)) * 0.002)
        side_mult = 1.0 if fut_side == "BUY" else -1.0
        r1 = float(entry_price) + side_mult * risk_pts
        r2 = float(entry_price) + side_mult * (2.0 * risk_pts)
        r3 = float(entry_price) + side_mult * (float(self.config.get("options_target3_r", 3.0)) * risk_pts)

        exit_mode = str(self.config.get("options_exit_mode") or "ladder_gtt").strip().lower()
        if exit_mode == "ladder_gtt" and float(opt_entry_price) > 0:
            premium_r = self._compute_option_premium_r(
                float(entry_price),
                float(initial_sl),
                float(atm.get("strike") or 0.0),
                int(atm.get("expiry_ms") or 0),
                opt_type,
                float(opt_entry_price),
            )
            self.option_positions[script_name] = {
                "parent_trade_id": futures_position.get("trade_id"),
                "futures_side": fut_side,
                "instrument_key": atm["instrument_key"],
                "trading_symbol": atm.get("trading_symbol"),
                "option_type": opt_type,
                "expiry_ms": int(atm.get("expiry_ms") or 0),
                "atm_strike": float(atm.get("strike") or 0.0),
                "entry_time": datetime.now().isoformat(),
                "entry_order_id": order_id,
                "entry_price_option": float(opt_entry_price),
                "entry_price_underlying": float(entry_price),
                "lot_size": lot_size,
                "total_lots": total_lots,
                "remaining_lots": total_lots,
                "booked_lots": 0,
                "gtt_ladder": True,
                "gtt_ids": {"sl": "", "tp1": "", "tp2": "", "tp3": ""},
                "premium_r": float(premium_r),
                "ladder_tp1_done": False,
                "ladder_tp2_done": False,
                "last_net_units": None,
                "initial_sl_underlying": float(initial_sl),
                "sl_underlying": float(initial_sl),
                "r1_underlying": float(r1),
                "r2_underlying": float(r2),
                "final_underlying": float(r3),
                "sl_option_price": 0.0,
                "active_sl_gtt_id": "",
            }
            sl_t = self._round_to_tick(float(opt_entry_price) - float(premium_r))
            tp1_t = self._round_to_tick(float(opt_entry_price) + float(premium_r))
            tp2_t = self._round_to_tick(float(opt_entry_price) + 2.0 * float(premium_r))
            tp3_t = self._round_to_tick(float(opt_entry_price) + 3.0 * float(premium_r))
            self._bot_logger.info(
                "OPTIONS LADDER ENTRY: %s %s key=%s qty=%s entry_opt=%.2f premium_R=%.2f "
                "SL_trig=%.2f TP1=%.2f TP2=%.2f TP3=%.2f fut_r1=%.2f",
                script_name,
                opt_type,
                atm["instrument_key"],
                qty,
                float(opt_entry_price),
                float(premium_r),
                sl_t,
                tp1_t,
                tp2_t,
                tp3_t,
                r1,
            )
            self._log_order_event(
                f"{script_name}_OPT",
                action="ENTRY",
                side="BUY",
                price=float(opt_entry_price),
                reason="FUTURES_SIGNAL_ATM_OPTION_LADDER",
                extra=(
                    f"symbol={atm.get('trading_symbol')}; order_id={order_id}; type={opt_type}; "
                    f"lots={total_lots}; lot_size={lot_size}; premium_R={premium_r:.2f}; "
                    f"fut_entry={entry_price:.2f}; fut_sl={initial_sl:.2f}; "
                    f"sl_trig={sl_t:.2f}; tp1={tp1_t:.2f}; tp2={tp2_t:.2f}; tp3={tp3_t:.2f}"
                ),
            )
            self._place_option_gtt_ladder(script_name, self.option_positions[script_name])
            return

        if float(opt_entry_price) <= 0:
            self._bot_logger.warning(
                "OPTIONS: %s LTP unavailable at entry — hybrid SL deferred until manage loop",
                script_name,
            )
        sl_opt = self._option_hybrid_sl_premium(
            float(entry_price), float(initial_sl), float(opt_entry_price or 0.0)
        )
        if float(opt_entry_price) <= 0:
            sl_opt = 0.0
        self.option_positions[script_name] = {
            "parent_trade_id": futures_position.get("trade_id"),
            "futures_side": fut_side,
            "instrument_key": atm["instrument_key"],
            "trading_symbol": atm.get("trading_symbol"),
            "option_type": opt_type,
            "expiry_ms": int(atm.get("expiry_ms") or 0),
            "entry_time": datetime.now().isoformat(),
            "entry_price_option": float(opt_entry_price),
            "entry_price_underlying": float(entry_price),
            "lot_size": lot_size,
            "total_lots": total_lots,
            "remaining_lots": total_lots,
            "booked_lots": 0,
            "r1_hit": False,
            "r2_hit": False,
            "final_hit": False,
            "options_rupee_tp_done": False,
            "gtt_ladder": False,
            "initial_sl_underlying": float(initial_sl),
            "sl_underlying": float(initial_sl),
            "sl_option_price": float(sl_opt),
            "r1_underlying": float(r1),
            "r2_underlying": float(r2),
            "final_underlying": float(r3),
            "active_sl_gtt_id": "",
        }
        self._bot_logger.info(
            f"OPTIONS ENTRY: {script_name} {opt_type} key={atm['instrument_key']} qty={qty} "
            f"entry_opt={float(opt_entry_price):.2f} hybrid_sl_opt={float(sl_opt):.2f} "
            f"r1={r1:.2f} r2={r2:.2f} final={r3:.2f} expiry_ms={atm.get('expiry_ms')}"
        )
        self._log_order_event(
            f"{script_name}_OPT",
            action="ENTRY",
            side="BUY",
            price=float(opt_entry_price),
            reason="FUTURES_SIGNAL_ATM_OPTION",
            extra=(
                f"symbol={atm.get('trading_symbol')}; type={opt_type}; lots={total_lots}; lot_size={lot_size}; "
                f"fut_entry={entry_price:.2f}; fut_sl={initial_sl:.2f}; hybrid_sl_opt={sl_opt:.2f}; "
                f"r1={r1:.2f}; r2={r2:.2f}; final={r3:.2f}; expiry_ms={atm.get('expiry_ms')}"
            ),
        )
        self._place_or_replace_option_sl_gtt(script_name, self.option_positions[script_name])

    def _close_option_lots(self, script_name: str, opt_pos: dict, lots_to_close: int, reason: str) -> bool:
        lot_size = int(opt_pos.get("lot_size", 1))
        rem = int(opt_pos.get("remaining_lots", 0))
        lots = min(max(0, int(lots_to_close)), rem)
        if lots <= 0:
            return False
        qty = max(1, lots * lot_size)
        res = self.client.place_order(str(opt_pos.get("instrument_key") or ""), qty, "SELL")
        if not res or res.get("status") != "success":
            self._bot_logger.error(
                f"OPTIONS EXIT FAILED: {script_name} lots={lots} qty={qty} reason={reason} err={(res or {}).get('error')}"
            )
            return False
        opt_ltp = self.client.get_ltp(str(opt_pos.get("instrument_key") or ""))
        if opt_ltp is None:
            opt_ltp = 0.0
        opt_pos["remaining_lots"] = rem - lots
        opt_pos["booked_lots"] = int(opt_pos.get("booked_lots", 0)) + lots
        self._log_order_event(
            f"{script_name}_OPT",
            action="EXIT",
            side="SELL",
            price=float(opt_ltp),
            reason=reason,
            extra=f"lots={lots}; remaining_lots={opt_pos.get('remaining_lots')}; instrument={opt_pos.get('trading_symbol')}",
        )
        return True

    def _close_all_option_for_script(self, script_name: str, reason: str, force_remove: bool = False) -> None:
        opt = self.option_positions.get(script_name)
        if not isinstance(opt, dict):
            return
        self._cancel_all_option_gtts(opt)
        rem = int(opt.get("remaining_lots", 0))
        if rem > 0:
            ok = self._close_option_lots(script_name, opt, rem, reason)
            if not ok and not force_remove:
                return
        self.option_positions.pop(script_name, None)

    def _sync_option_sl_from_underlying_model(self, script_name: str, opt: dict) -> None:
        """Ratchet option GTT trigger up when futures trailing SL tightens (delta-mapped premium)."""
        if not bool(self.config.get("options_gtt_enabled", True)):
            return
        entry_opt = self._safe_float(opt.get("entry_price_option"), 0.0)
        entry_u = self._safe_float(opt.get("entry_price_underlying"), 0.0)
        sl_u = self._safe_float(opt.get("sl_underlying"), 0.0)
        if entry_opt <= 0 or entry_u <= 0:
            return
        cand = self._option_hybrid_sl_premium(entry_u, sl_u, entry_opt)
        prev = self._safe_float(opt.get("sl_option_price"), 0.0)
        new_sl = max(prev, cand)
        if new_sl > prev + 0.04:
            opt["sl_option_price"] = float(new_sl)
            self._place_or_replace_option_sl_gtt(script_name, opt)
            self._bot_logger.info(
                "OPTIONS SL SYNC: %s sl_opt %.2f -> %.2f (fut_sl=%.2f)",
                script_name,
                prev,
                new_sl,
                sl_u,
            )

    def _manage_option_positions(self, latest_prices: dict[str, float]) -> None:
        if not self.option_positions:
            return
        for script_name in list(self.option_positions.keys()):
            opt = self.option_positions.get(script_name)
            if not isinstance(opt, dict):
                continue
            if script_name not in self.positions:
                self._close_all_option_for_script(script_name, "FUTURES_POSITION_CLOSED", force_remove=True)
                continue
            px = latest_prices.get(script_name)
            if px is None:
                continue
            side = str(opt.get("futures_side") or "").upper()
            if side not in {"BUY", "SELL"}:
                continue
            if opt.get("gtt_ladder"):
                if self._option_chart_crossover_should_exit(opt):
                    self._bot_logger.info("OPTIONS LADDER CHART EXIT: %s", script_name)
                    self._close_all_option_for_script(
                        script_name, "OPT_CHART_CROSSOVER", force_remove=True
                    )
                    continue
                self._refresh_ladder_sl_gtt_from_futures(script_name, opt)
                self._reconcile_ladder_partial_fills(script_name, opt)
                ikey = str(opt.get("instrument_key") or "")
                netu = self._net_option_units_for_instrument(ikey)
                if netu is not None and netu <= 0:
                    self._cancel_all_option_gtts(opt)
                    self.option_positions.pop(script_name, None)
                    self._bot_logger.info(
                        "OPTIONS LADDER: flat at broker; cleared state %s", script_name
                    )
                continue
            if self._safe_float(opt.get("entry_price_option"), 0.0) <= 0:
                seed_ltp = self.client.get_ltp(str(opt.get("instrument_key") or ""))
                if seed_ltp is not None and seed_ltp > 0:
                    opt["entry_price_option"] = float(seed_ltp)
                    opt["sl_option_price"] = self._option_hybrid_sl_premium(
                        self._safe_float(opt.get("entry_price_underlying"), 0.0),
                        self._safe_float(opt.get("sl_underlying"), 0.0),
                        float(seed_ltp),
                    )
                    self._bot_logger.info(
                        "OPTIONS DEFER ENTRY: %s entry_opt=%.2f sl_opt=%.2f (first LTP)",
                        script_name,
                        float(seed_ltp),
                        float(opt["sl_option_price"]),
                    )
                    self._place_or_replace_option_sl_gtt(script_name, opt)
            self._sync_option_sl_from_underlying_model(script_name, opt)

            tgt = self._options_scaled_rupee_profit_target(opt)
            ob = self.config.get("options_rupee_profit_booking") or {}
            if (
                tgt is not None
                and not bool(opt.get("options_rupee_tp_done"))
                and int(opt.get("remaining_lots", 0)) > 0
            ):
                opt_ltp = self.client.get_ltp(str(opt.get("instrument_key") or ""))
                if opt_ltp is not None and opt_ltp > 0:
                    entry_opt = self._safe_float(opt.get("entry_price_option"), 0.0)
                    if entry_opt > 0:
                        rem_lots = int(opt.get("remaining_lots", 0))
                        lot_sz = int(opt.get("lot_size", 1))
                        qty_open = max(0, rem_lots * lot_sz)
                        mfe_pnl = (float(opt_ltp) - entry_opt) * float(qty_open)
                        if mfe_pnl >= float(tgt):
                            frac = float(ob.get("lot_fraction", 0.25))
                            lots_close = int(math.ceil(rem_lots * max(0.05, min(1.0, frac))))
                            lots_close = max(1, min(lots_close, rem_lots))
                            if self._close_option_lots(script_name, opt, lots_close, "OPT_RUPEE_TP"):
                                opt["options_rupee_tp_done"] = True
                                self._bot_logger.info(
                                    "OPTIONS RUPEE TP: %s booked %d lots mfe_pnl=%.2f target=%.2f",
                                    script_name,
                                    lots_close,
                                    mfe_pnl,
                                    float(tgt),
                                )
                                self._place_or_replace_option_sl_gtt(script_name, opt)

            r1 = self._safe_float(opt.get("r1_underlying"), 0.0)
            r2 = self._safe_float(opt.get("r2_underlying"), 0.0)
            r3 = self._safe_float(opt.get("final_underlying"), 0.0)
            hit_r1 = px >= r1 if side == "BUY" else px <= r1
            hit_r2 = px >= r2 if side == "BUY" else px <= r2
            hit_r3 = px >= r3 if side == "BUY" else px <= r3

            if (not bool(opt.get("r1_hit"))) and hit_r1:
                if self._close_option_lots(script_name, opt, 2, "OPT_TP_1R"):
                    opt["r1_hit"] = True
                    opt["sl_underlying"] = float(opt.get("entry_price_underlying", px)) + (
                        float(self.config.get("options_breakeven_buffer_points", 0.0)) * (1.0 if side == "BUY" else -1.0)
                    )
                    opt_ltp = self.client.get_ltp(str(opt.get("instrument_key") or ""))
                    if opt_ltp is not None and opt_ltp > 0:
                        prev_sl = self._safe_float(opt.get("sl_option_price"), 0.0)
                        opt["sl_option_price"] = max(prev_sl, float(opt_ltp))
                    self._place_or_replace_option_sl_gtt(script_name, opt)

            if (not bool(opt.get("r2_hit"))) and hit_r2:
                if self._close_option_lots(script_name, opt, 1, "OPT_TP_2R"):
                    opt["r2_hit"] = True
                    opt["sl_underlying"] = float(opt.get("r1_underlying", px))
                    self._place_or_replace_option_sl_gtt(script_name, opt)

            if (not bool(opt.get("final_hit"))) and hit_r3:
                if self._close_option_lots(script_name, opt, int(opt.get("remaining_lots", 0)), "OPT_TP_FINAL"):
                    opt["final_hit"] = True
                    self._close_all_option_for_script(script_name, "OPT_CYCLE_DONE", force_remove=True)
                    continue

            if int(opt.get("remaining_lots", 0)) <= 0:
                self.option_positions.pop(script_name, None)

    @staticmethod
    def _stoploss_reason(position):
        """
        Return stop-loss reason code.
        If SL has moved away from initial SL, treat it as trailing SL hit.
        """
        initial_sl = position.get('initial_sl')
        current_sl = position.get('stop_loss')
        if initial_sl is None or current_sl is None:
            return "STOP_LOSS_HIT"

        if abs(float(current_sl) - float(initial_sl)) > 1e-9:
            return "TRAILING_STOP_LOSS_HIT"
        return "STOP_LOSS_HIT"

    @staticmethod
    def _is_mcx_instrument(instrument_key):
        return isinstance(instrument_key, str) and instrument_key.startswith("MCX_FO|")

    def _should_attempt_contract_roll(self, script_name):
        cooldown = float(self.config.get("contract_roll_retry_seconds", 300))
        now_ts = time.time()
        last_attempt = float(self._last_contract_roll_attempt.get(script_name, 0.0))
        if now_ts - last_attempt < cooldown:
            return False
        self._last_contract_roll_attempt[script_name] = now_ts
        return True

    def _fetch_mcx_instruments(self):
        cache_ttl = float(self.config.get("contract_roll_mcx_cache_seconds", 21600))
        now_ts = time.time()
        if self._mcx_instruments_cache and (now_ts - self._mcx_instruments_cache_at) < cache_ttl:
            return self._mcx_instruments_cache

        response = requests.get(MCX_INSTRUMENTS_URL, timeout=20)
        response.raise_for_status()
        instruments = json.loads(gzip.decompress(response.content))
        self._mcx_instruments_cache = instruments if isinstance(instruments, list) else []
        self._mcx_instruments_cache_at = now_ts
        return self._mcx_instruments_cache

    def _fetch_exchange_instruments(self, url: str, cache_key: str):
        """
        Fetch Upstox instrument master (gz JSON) for a specific exchange.
        Caches separately for NSE/BSE to support contract rollover for futures.
        """
        cache_ttl = float(self.config.get("contract_roll_mcx_cache_seconds", 21600))
        now_ts = time.time()
        if cache_key == "nse":
            if self._nse_instruments_cache and (now_ts - self._nse_instruments_cache_at) < cache_ttl:
                return self._nse_instruments_cache
        if cache_key == "bse":
            if self._bse_instruments_cache and (now_ts - self._bse_instruments_cache_at) < cache_ttl:
                return self._bse_instruments_cache

        response = requests.get(url, timeout=30)
        response.raise_for_status()
        instruments = json.loads(gzip.decompress(response.content))
        if not isinstance(instruments, list):
            instruments = []

        if cache_key == "nse":
            self._nse_instruments_cache = instruments
            self._nse_instruments_cache_at = now_ts
        elif cache_key == "bse":
            self._bse_instruments_cache = instruments
            self._bse_instruments_cache_at = now_ts
        return instruments

    def _list_fo_futures_candidates_sorted(self, script_name: str) -> list[tuple[int, str]]:
        """
        Active NSE_FO/BSE_FO futures for this root, sorted by expiry then instrument_key.
        Returns (expiry_ms, instrument_key) with unique keys.
        """
        seg = "BSE_FO" if script_name == "SENSEX" else "NSE_FO"
        exchange = "bse" if script_name == "SENSEX" else "nse"
        roots = [str(script_name or "").strip().upper()]
        if not roots[0]:
            return []

        try:
            instruments = self._fetch_exchange_instruments(
                BSE_INSTRUMENTS_URL if exchange == "bse" else NSE_INSTRUMENTS_URL,
                exchange,
            )
        except Exception as e:
            self._bot_logger.warning(
                f"WARNING: Unable to fetch {exchange.upper()} instruments for contract roll: {e}"
            )
            return []

        target_lot = int(self.config.get("lot_sizes", {}).get(script_name, 0))
        now_ms = int(time.time() * 1000)

        candidates: list[tuple[int, str]] = []
        for row in instruments:
            instrument_type = str(row.get("instrument_type", "")).upper()
            if instrument_type != "FUT":
                continue

            trading_symbol = str(row.get("trading_symbol", "")).upper().strip()
            first_tok = trading_symbol.split()[0] if trading_symbol else ""
            if first_tok not in roots:
                continue

            row_seg = str(row.get("segment", "")).upper()
            if row_seg != seg:
                continue

            instrument_key = str(row.get("instrument_key", ""))
            if not instrument_key.startswith(f"{seg}|"):
                continue

            expiry_ms = int(row.get("expiry", 0) or 0)
            if expiry_ms and expiry_ms < now_ms:
                continue

            lot_size = int(float(row.get("lot_size", 0) or 0))
            if target_lot and lot_size != target_lot:
                continue

            candidates.append((expiry_ms, instrument_key))

        candidates.sort(key=lambda item: (item[0], item[1]))
        out: list[tuple[int, str]] = []
        seen: set[str] = set()
        for ms, key in candidates:
            if key in seen:
                continue
            seen.add(key)
            out.append((ms, key))
        return out

    def _get_fo_contract_candidates(self, script_name: str) -> list[str]:
        """Candidate keys for NSE_FO/BSE_FO rollovers (front month first)."""
        return [key for _, key in self._list_fo_futures_candidates_sorted(script_name)]

    def _select_index_fo_contract_avoiding_expiring_front(self, script_name: str) -> str | None:
        """
        On the front contract's expiry day, trade the next serial month for index futures
        (NIFTY / BANKNIFTY / SENSEX) so entries are not pinned to the expiring lot.
        """
        rows = self._list_fo_futures_candidates_sorted(script_name)
        if not rows:
            return None
        if script_name not in _INDEX_FO_SCRIPT_NAMES:
            return rows[0][1]
        now_ist = self._now_ist()
        today = now_ist.date()
        front_ms, front_key = rows[0]
        if _ist_date_from_expiry_ms(front_ms) == today:
            if len(rows) > 1:
                self._bot_logger.info(
                    "INDEX FUTURES: %s expiry day — selecting next serial contract (skip expiring front %s)",
                    script_name,
                    front_key,
                )
                return rows[1][1]
            self._bot_logger.warning(
                "INDEX FUTURES: %s expiry day but only one contract in chain; using %s",
                script_name,
                front_key,
            )
        return front_key

    def _maybe_refresh_index_futures_tokens_expiry_day(self) -> None:
        """If live config still references today's expiring front-month index future, advance to next."""
        now_ts = time.time()
        if now_ts - self._last_index_fo_token_refresh_ts < 120.0:
            return
        self._last_index_fo_token_refresh_ts = now_ts
        for script_name in _INDEX_FO_SCRIPT_NAMES:
            rows = self._list_fo_futures_candidates_sorted(script_name)
            if len(rows) < 2:
                continue
            front_ms, front_key = rows[0]
            if _ist_date_from_expiry_ms(front_ms) != self._now_ist().date():
                continue
            cur = str(self._get_order_token(script_name) or "").strip()
            if cur and cur != front_key:
                continue
            _, next_key = rows[1]
            self._bot_logger.info(
                "INDEX FUTURES ROLL: %s token %s -> %s (expiry-day next serial)",
                script_name,
                front_key,
                next_key,
            )
            self.config.setdefault("scripts", {})[script_name] = next_key
            self.config.setdefault("order_tokens", {})[script_name] = next_key

    def _get_mcx_contract_candidates(self, script_name):
        script_roots = {
            "CRUDE": ["CRUDEOIL"],
            "GOLDMINI": ["GOLDPETAL", "GOLDM"],
            "SILVERMINI": ["SILVERM"],
        }
        roots = script_roots.get(script_name, [])
        if not roots:
            return []

        try:
            instruments = self._fetch_mcx_instruments()
        except Exception as e:
            self._bot_logger.warning(f"WARNING: Unable to fetch MCX instruments for contract roll: {e}")
            return []

        target_lot = int(self.config.get("lot_sizes", {}).get(script_name, 0))
        now_ms = int(time.time() * 1000)
        candidates = []
        for row in instruments:
            if str(row.get("instrument_type", "")).upper() != "FUT":
                continue

            instrument_key = str(row.get("instrument_key", ""))
            if not instrument_key.startswith("MCX_FO|"):
                continue

            expiry_ms = int(row.get("expiry", 0) or 0)
            if expiry_ms and expiry_ms < now_ms:
                continue

            lot_size = int(float(row.get("lot_size", 0) or 0))
            if target_lot and lot_size != target_lot:
                continue

            trading_symbol = str(row.get("trading_symbol", "")).upper()
            if not any(trading_symbol.startswith(f"{root} ") for root in roots):
                continue

            candidates.append((expiry_ms, instrument_key, trading_symbol))

        candidates.sort(key=lambda item: (item[0], item[1]))

        unique_keys = []
        seen = set()
        for _, key, _ in candidates:
            if key not in seen:
                seen.add(key)
                unique_keys.append(key)
        return unique_keys

    def _switch_to_next_contract(self, script_name, current_instrument_key):
        # Determine candidate list based on current instrument family.
        if self._is_mcx_instrument(current_instrument_key):
            candidates = self._get_mcx_contract_candidates(script_name)
        elif isinstance(current_instrument_key, str) and current_instrument_key.startswith(("NSE_FO|", "BSE_FO|")):
            candidates = self._get_fo_contract_candidates(script_name)
        else:
            # Fallback: try FO roll for known NSE/BSE scripts.
            candidates = self._get_fo_contract_candidates(script_name) or self._get_mcx_contract_candidates(script_name)

        if not candidates:
            return current_instrument_key

        if current_instrument_key in candidates:
            current_idx = candidates.index(current_instrument_key)
            if current_idx + 1 < len(candidates):
                next_key = candidates[current_idx + 1]
            else:
                next_key = current_instrument_key
        else:
            next_key = candidates[0]

        if next_key == current_instrument_key:
            return current_instrument_key

        self.config.setdefault("scripts", {})[script_name] = next_key
        self.config.setdefault("order_tokens", {})[script_name] = next_key
        self._bot_logger.warning(
            f"CONTRACT ROLL: {script_name} switched from {current_instrument_key} to {next_key}"
        )
        return next_key

    @staticmethod
    def _calculate_ob_percent(entry_price, stop_loss):
        if entry_price is None or stop_loss is None or entry_price <= 0:
            return 0.0
        return abs((entry_price - stop_loss) / entry_price) * 100

    def _get_min_ob_percent(self, script_name):
        return float(self.config.get('min_ob_percent_by_script', {}).get(script_name, 0.0))

    def _get_min_ema_separation_percent(self, script_name):
        per_script = self.config.get('min_ema_separation_percent_by_script', {})
        if script_name in per_script:
            return float(per_script[script_name])
        return float(self.config.get('min_ema_separation_percent', 0.03))

    def _compute_percent_level_metrics(self, df, anchor_timestamp, reference_price):
        if (
            df is None
            or df.empty
            or anchor_timestamp is None
            or reference_price is None
            or reference_price <= 0
            or 'timestamp' not in df.columns
            or 'high' not in df.columns
            or 'low' not in df.columns
        ):
            return None

        working = df.sort_values('timestamp').reset_index(drop=True)
        eligible = working[working['timestamp'] <= anchor_timestamp]
        if eligible.empty:
            return None

        lookback = int(self.config.get('percent_levels_lookback_candles', 60))
        window = eligible.tail(max(5, lookback))
        if window.empty:
            return None

        swing_low = float(window['low'].min())
        swing_high = float(window['high'].max())
        swing_range = swing_high - swing_low
        if swing_range <= 0:
            return None

        levels = self.config.get('percent_levels_to_log', [19.43, 33.66, 46.91])
        level_rows = []
        for raw in levels:
            pct = float(raw)
            lvl_price = swing_low + (swing_range * (pct / 100.0))
            dist_pct = ((reference_price - lvl_price) / reference_price) * 100.0
            level_rows.append({"pct": pct, "price": lvl_price, "dist_pct": dist_pct})

        return {
            "swing_low": swing_low,
            "swing_high": swing_high,
            "swing_range": swing_range,
            "levels": level_rows,
        }

    def _compute_chart_ob_snapshot(self, df, anchor_timestamp, side):
        """
        Pine-parity OB% snapshot (BigBeluga strategy variant).
        - Rebuilds lower/upper OB arrays from start up to anchor timestamp.
        - Matches TradingView **on-chart** OB labels at the anchor bar (barstate.islast style):
          newest active OB volume / sum(all active same-side OB volumes) * 100.
          (Percentages update as OBs are invalidated; formation-only % would drift vs TV.)
        - Fallback when no active OBs on that side: (None, None).
        Returns: (chart_percent, selected_ob_volume)
        """
        if (
            df is None
            or df.empty
            or anchor_timestamp is None
            or 'timestamp' not in df.columns
            or 'open' not in df.columns
            or 'close' not in df.columns
            or 'high' not in df.columns
            or 'low' not in df.columns
            or 'volume' not in df.columns
            or 'crossover' not in df.columns
            or 'signal' not in df.columns
        ):
            return None, None

        working = df.sort_values('timestamp').reset_index(drop=True)
        eligible = working[working['timestamp'] <= anchor_timestamp]
        if eligible.empty:
            return None, None

        n = len(eligible)
        length1 = max(1, int(self.config.get('ema_short', 5)))
        length2 = length1 + 13
        max_active = max(1, int(self.config.get('chart_ob_max_active_per_side', 15)))

        o = eligible['open'].astype(float)
        h = eligible['high'].astype(float)
        l = eligible['low'].astype(float)
        c = eligible['close'].astype(float)
        v = eligible['volume'].astype(float).abs()

        ema1 = c.ewm(span=length1, adjust=False).mean()
        ema2 = c.ewm(span=length2, adjust=False).mean()
        lowest = l.rolling(window=length2, min_periods=1).min()
        highest = h.rolling(window=length2, min_periods=1).max()

        prev_close = c.shift(1)
        tr = pd.concat(
            [
                (h - l),
                (h - prev_close).abs(),
                (l - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        # Pine ta.atr(200) uses Wilder smoothing (RMA), not a simple SMA of TR.
        atr200 = tr.ewm(alpha=1.0 / 200.0, adjust=False).mean()
        atr_hi_200 = atr200.rolling(window=200, min_periods=1).max()
        atr = atr_hi_200 * 3.0
        atr1 = atr_hi_200 * 2.0

        upper_lvl = []
        lower_lvl = []

        def _price_eq(a, b, rel_tol=1e-9, abs_tol=1e-12):
            return a == b or math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=abs_tol)

        def _newest_active_ob_share(levels):
            """Same as TV chart labels: newest OB's vol / sum(active side vols) * 100."""
            active = [
                ob for ob in levels
                if ob is not None and ob.get("vol") is not None
            ]
            if not active:
                return None, None
            newest = active[-1]
            total = sum(float(ob["vol"]) for ob in active)
            if total <= 0:
                return None, None
            v_new = float(newest["vol"])
            pct = round((v_new / total) * 100.0, 2)
            return pct, v_new

        def _cleanup_side(levels, is_lower, close_price, atr_val):
            if len(levels) > 1:
                for i in range(1, len(levels)):
                    cur = levels[i]
                    prev = levels[i - 1]
                    if cur is None or prev is None:
                        continue

                    if abs(float(cur["mid"]) - float(prev["mid"])) < float(atr_val):
                        levels[i - 1] = None

                    if is_lower:
                        if close_price < float(cur["lower"]):
                            levels[i] = None
                    else:
                        if close_price > float(cur["upper"]):
                            levels[i] = None

                if len(levels) > max_active:
                    levels.pop(0)

        for idx in range(1, n):
            cross_up = (
                pd.notna(ema1.iloc[idx - 1])
                and pd.notna(ema2.iloc[idx - 1])
                and float(ema1.iloc[idx - 1]) <= float(ema2.iloc[idx - 1])
                and float(ema1.iloc[idx]) > float(ema2.iloc[idx])
            )
            cross_dn = (
                pd.notna(ema1.iloc[idx - 1])
                and pd.notna(ema2.iloc[idx - 1])
                and float(ema1.iloc[idx - 1]) >= float(ema2.iloc[idx - 1])
                and float(ema1.iloc[idx]) < float(ema2.iloc[idx])
            )

            if cross_up:
                found = False
                for i in range(1, length2 + 1):
                    j = idx - i
                    if j < 0:
                        break
                    if _price_eq(l.iloc[j], lowest.iloc[idx]) and not found:
                        ob_vol = float(v.iloc[j:idx + 1].sum())
                        src = min(float(o.iloc[j]), float(c.iloc[j]))
                        low_ref = float(lowest.iloc[idx])
                        atr1_ref = float(atr1.iloc[idx])
                        if (src - low_ref) < (atr1_ref * 0.5):
                            src = low_ref + (atr1_ref * 0.5)
                        mid = (src + low_ref) / 2.0
                        lower_lvl.append(
                            {"upper": src, "lower": low_ref, "mid": mid, "vol": ob_vol}
                        )
                        found = True

            if cross_dn:
                found = False
                for i in range(1, length2 + 1):
                    j = idx - i
                    if j < 0:
                        break
                    if _price_eq(h.iloc[j], highest.iloc[idx]) and not found:
                        ob_vol = float(v.iloc[j:idx + 1].sum())
                        src = max(float(o.iloc[j]), float(c.iloc[j]))
                        high_ref = float(highest.iloc[idx])
                        atr1_ref = float(atr1.iloc[idx])
                        if (high_ref - src) < (atr1_ref * 0.5):
                            src = high_ref - (atr1_ref * 0.5)
                        mid = (src + high_ref) / 2.0
                        upper_lvl.append(
                            {"upper": high_ref, "lower": src, "mid": mid, "vol": ob_vol}
                        )
                        found = True

            close_price = float(c.iloc[idx])
            atr_val = float(atr.iloc[idx])
            _cleanup_side(lower_lvl, True, close_price, atr_val)
            _cleanup_side(upper_lvl, False, close_price, atr_val)

        wanted = "BUY" if str(side).upper() == "BUY" else "SELL"
        if wanted == "BUY":
            return _newest_active_ob_share(lower_lvl)
        return _newest_active_ob_share(upper_lvl)

    def _compute_chart_ob_percent(self, df, entry_candle_timestamp, side):
        chart_percent, _ = self._compute_chart_ob_snapshot(df, entry_candle_timestamp, side)
        return chart_percent

    def _build_percent_levels_context(self, level_metrics):
        """
        Build a compact context string for key percentage levels (e.g., 19.43/33.66/46.91).
        Useful in ENTRY/SKIP logs for later trade selection analysis.
        """
        if not level_metrics:
            return ""

        parts = [
            f"range_low={level_metrics['swing_low']:.2f}",
            f"range_high={level_metrics['swing_high']:.2f}",
            f"range_pts={level_metrics['swing_range']:.2f}",
        ]
        for row in level_metrics.get('levels', []):
            pct = row["pct"]
            pct_tag = str(f"{pct:.2f}").replace(".", "_")
            parts.append(f"lvl_{pct_tag}={row['price']:.2f}")
            parts.append(f"dist_{pct_tag}={row['dist_pct']:+.3f}%")

        return "; ".join(parts)

    def _estimate_trade_probability(
        self,
        script_name,
        ema_slope_ok,
        ema_sep_pct,
        min_sep_pct,
        ob_percent,
        level_metrics,
    ):
        weights = self.config.get('trade_probability_weights', {})
        w_slope = float(weights.get('ema_slope', 0.25))
        w_sep = float(weights.get('ema_sep', 0.25))
        w_ob = float(weights.get('ob_quality', 0.30))
        w_lvl = float(weights.get('level_proximity', 0.20))
        weight_sum = max(1e-9, (w_slope + w_sep + w_ob + w_lvl))

        slope_score = 100.0 if ema_slope_ok else 0.0

        if min_sep_pct > 0:
            # Need materially strong separation (not just barely above threshold).
            sep_score = max(0.0, min(100.0, (ema_sep_pct / (min_sep_pct * 1.8)) * 100.0))
        else:
            sep_score = 100.0

        min_ob_pct = max(1e-9, self._get_min_ob_percent(script_name))
        ob_raw = float(ob_percent or 0.0)
        # Avoid inflating score when OB% is only slightly above minimum.
        ob_score = max(0.0, min(100.0, (ob_raw / (min_ob_pct * 2.5)) * 100.0))

        level_score = 35.0
        has_level_context = bool(level_metrics and level_metrics.get('levels'))
        if has_level_context:
            ref_pct = float(self.config.get('trade_probability_reference_level_percent', 33.66))
            nearest = min(
                level_metrics['levels'],
                key=lambda r: abs(float(r['pct']) - ref_pct)
            )
            ref_dist = abs(float(nearest['dist_pct']))
            # 0% away => 100 score, 4%+ away => 0 score
            level_score = max(0.0, min(100.0, 100.0 - (ref_dist * 25.0)))

        weighted = (
            (slope_score * w_slope)
            + (sep_score * w_sep)
            + (ob_score * w_ob)
            + (level_score * w_lvl)
        ) / weight_sum
        if not has_level_context:
            weighted *= 0.72
        if not ema_slope_ok:
            weighted = min(weighted, 45.0)

        probability = round(max(0.0, min(100.0, weighted)), 1)
        if not has_level_context:
            probability = min(probability, 65.0)
        bucket = "HIGH" if probability >= 70 else ("MEDIUM" if probability >= 50 else "LOW")
        return probability, bucket

    def _get_adx_min_threshold(self, script_name):
        by_script = self.config.get('adx_min_threshold_by_script', {})
        if script_name in by_script:
            return float(by_script[script_name])
        return float(self.config.get('adx_min_threshold', 20.0))

    @staticmethod
    def _calculate_adx_values(df, period=14):
        """
        Wilder-style ADX and DI series.
        Returns (adx, plus_di, minus_di) aligned to df index.
        """
        high = df['high'].astype(float)
        low = df['low'].astype(float)
        close = df['close'].astype(float)

        up_move = high.diff()
        down_move = -low.diff()

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        plus_dm = pd.Series(plus_dm, index=df.index)
        minus_dm = pd.Series(minus_dm, index=df.index)

        prev_close = close.shift(1)
        tr = pd.concat(
            [
                (high - low),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        alpha = 1.0 / max(1, int(period))
        atr = tr.ewm(alpha=alpha, adjust=False).mean()
        plus_dm_sm = plus_dm.ewm(alpha=alpha, adjust=False).mean()
        minus_dm_sm = minus_dm.ewm(alpha=alpha, adjust=False).mean()

        plus_di = 100.0 * (plus_dm_sm / atr.replace(0, np.nan))
        minus_di = 100.0 * (minus_dm_sm / atr.replace(0, np.nan))
        dx = 100.0 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
        adx = dx.ewm(alpha=alpha, adjust=False).mean()

        return (
            adx.fillna(0.0),
            plus_di.fillna(0.0),
            minus_di.fillna(0.0),
        )

    def _now_ist(self):
        return datetime.now(ZoneInfo("Asia/Kolkata"))

    def _script_segment(self, script_name):
        segment_scripts = self.config.get('segment_scripts', {})
        for segment, scripts in segment_scripts.items():
            if script_name in scripts:
                return segment
        return None

    def _nse_rupee_sl_target_prices(self, script_name, position_type, entry_price, quantity):
        cfg = self.config.get("nse_trade_pnl_levels", {}) or {}
        if not bool(cfg.get("enabled", True)):
            return None, None
        scripts = cfg.get("scripts") or self.config.get("segment_scripts", {}).get("NSE", [])
        if not script_name or script_name not in scripts:
            return None, None
        if entry_price is None or float(entry_price) <= 0:
            return None, None
        qty = float(quantity or 0.0)
        if qty <= 0:
            return None, None

        target_pnl = float(cfg.get("target_pnl", 5000.0))
        stop_loss_pnl = float(cfg.get("stop_loss_pnl", 3000.0))
        if target_pnl <= 0 or stop_loss_pnl <= 0:
            return None, None

        entry = float(entry_price)
        if position_type == "BUY":
            return entry - (stop_loss_pnl / qty), entry + (target_pnl / qty)
        if position_type == "SELL":
            return entry + (stop_loss_pnl / qty), entry - (target_pnl / qty)
        return None, None

    def _segment_cutoff_dt(self, segment, now_ist):
        squareoff_times = self.config.get('eod_squareoff_times', {})
        cutoff_text = squareoff_times.get(segment)
        if not cutoff_text or ':' not in cutoff_text:
            return None

        hour_text, minute_text = cutoff_text.split(':', 1)
        hour = int(hour_text)
        minute = int(minute_text)
        return now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _segment_entry_start_dt(self, segment, now_ist):
        start_times = self.config.get('entry_start_times', {})
        start_text = start_times.get(segment)
        if not start_text or ':' not in start_text:
            return None

        hour_text, minute_text = start_text.split(':', 1)
        hour = int(hour_text)
        minute = int(minute_text)
        return now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _daily_shutdown_dt(self, now_ist):
        shutdown_text = self.config.get('daily_shutdown_time', '23:21')
        if not shutdown_text or ':' not in shutdown_text:
            return None

        hour_text, minute_text = shutdown_text.split(':', 1)
        hour = int(hour_text)
        minute = int(minute_text)
        return now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _is_after_daily_shutdown(self, now_ist):
        shutdown_dt = self._daily_shutdown_dt(now_ist)
        if shutdown_dt is None:
            return False
        return now_ist >= shutdown_dt

    def _run_daily_archive(self):
        """Run archive_day.py; snapshots land under src/server/data/users/<this user>/archive/."""
        old = os.environ.get("TRADING_USER")
        try:
            os.environ["TRADING_USER"] = self.username
            import archive_day

            archive_day.main()
        except Exception as e:
            print(f"ERROR: Daily archive failed ({self.username}): {e}")
        finally:
            if old is None:
                os.environ.pop("TRADING_USER", None)
            else:
                os.environ["TRADING_USER"] = old

    def _is_before_segment_entry_start(self, script_name, now_ist):
        segment = self._script_segment(script_name)
        if not segment:
            return False

        start_dt = self._segment_entry_start_dt(segment, now_ist)
        if start_dt is None:
            return False

        return now_ist < start_dt

    def _is_after_segment_cutoff(self, script_name, now_ist):
        segment = self._script_segment(script_name)
        if not segment:
            return False

        cutoff_dt = self._segment_cutoff_dt(segment, now_ist)
        if cutoff_dt is None:
            return False

        return now_ist >= cutoff_dt

    def _run_eod_squareoff(self, now_ist, latest_prices=None):
        segment_scripts = self.config.get('segment_scripts', {})
        today_text = now_ist.strftime('%Y-%m-%d')
        latest_prices = latest_prices or {}

        for segment, scripts in segment_scripts.items():
            cutoff_dt = self._segment_cutoff_dt(segment, now_ist)
            if cutoff_dt is None or now_ist < cutoff_dt:
                continue

            if self.eod_squareoff_done.get(segment) == today_text:
                continue

            self._bot_logger.info(f"EOD: Square-off check for {segment} at {now_ist.strftime('%H:%M:%S')}")
            any_closed = False

            for script_name in scripts:
                if script_name in self.paper_positions:
                    position = self.paper_positions[script_name]
                    self._ensure_position_fields(position, script_name)
                    exit_side = "SELL" if position.get("type") == "BUY" else "BUY"
                    market_price = latest_prices.get(script_name)
                    price_source = "ltp"
                    if market_price is None:
                        market_price = position.get("entry_price", 0.0)
                        price_source = "entry_fallback"
                    self._paper_exit_after_signal(
                        script_name,
                        position,
                        exit_side,
                        float(market_price),
                        "EOD_SQUAREOFF",
                        extra_log=(
                            f"cutoff={cutoff_dt.strftime('%H:%M')}; price_source={price_source}; "
                            f"entry_adx={float(position.get('signal_adx', 0.0) or 0.0):.2f}; "
                            f"plus_di={float(position.get('signal_plus_di', 0.0) or 0.0):.2f}; "
                            f"minus_di={float(position.get('signal_minus_di', 0.0) or 0.0):.2f}"
                        ),
                    )
                    any_closed = True
                    continue

                if script_name not in self.positions:
                    continue

                position = self.positions[script_name]
                self._ensure_position_fields(position, script_name)
                exit_side = "SELL" if position.get('type') == 'BUY' else "BUY"
                market_price = latest_prices.get(script_name)
                price_source = "ltp"
                if market_price is None:
                    market_price = position.get('entry_price', 0.0)
                    price_source = "entry_fallback"

                success, order_result = self._place_order_with_result(
                    script_name,
                    exit_side,
                    market_price,
                    "EOD_SQUAREOFF",
                    realized_pnl=self._calculate_realized_pnl(
                        position.get('type', 'BUY'),
                        float(position.get('entry_price', market_price)),
                        float(market_price),
                        float(position.get('quantity', self._get_order_quantity(script_name)))
                    ),
                    entry_adx=float(position.get('signal_adx', 0.0) or 0.0),
                    entry_plus_di=float(position.get('signal_plus_di', 0.0) or 0.0),
                    entry_minus_di=float(position.get('signal_minus_di', 0.0) or 0.0),
                )
                if not success:
                    continue

                order_id = (order_result.get('data') or {}).get('order_id', 'NA')
                self._log_order_event(
                    script_name,
                    action="EXIT",
                    side=exit_side,
                    price=market_price,
                    reason="EOD_SQUAREOFF",
                    extra=(
                        f"cutoff={cutoff_dt.strftime('%H:%M')}; "
                        f"price_source={price_source}; "
                        f"order_id={order_id}; "
                        f"entry_adx={float(position.get('signal_adx', 0.0) or 0.0):.2f}; "
                        f"plus_di={float(position.get('signal_plus_di', 0.0) or 0.0):.2f}; "
                        f"minus_di={float(position.get('signal_minus_di', 0.0) or 0.0):.2f}"
                    )
                )
                self._notify_dashboard_trade_close(script_name, position, market_price)
                del self.positions[script_name]
                any_closed = True

            remaining = [
                s for s in scripts
                if s in self.positions or s in self.paper_positions
            ]
            if not remaining:
                self.eod_squareoff_done[segment] = today_text
                self._bot_logger.info(f"EOD: {segment} square-off completed for {today_text}")
                self.save_state()
            elif any_closed:
                self.save_state()
                self._bot_logger.warning(f"EOD: {segment} square-off partial. Remaining: {remaining}")

    def _favorable_move_percent(self, position_type, entry_price, current_price):
        if position_type == 'BUY':
            return ((current_price - entry_price) / entry_price) * 100
        return ((entry_price - current_price) / entry_price) * 100

    def _post_entry_trailing_reference_price(self, position, data, current_price):
        """
        Trailing reference from post-entry range only.
        - BUY: highest high seen after entry timestamp
        - SELL: lowest low seen after entry timestamp
        Falls back to current_price when unavailable.
        """
        try:
            side = str(position.get("type") or "").upper()
            if side not in ("BUY", "SELL"):
                return float(current_price)

            anchor_raw = (
                position.get("signal_time")
                or position.get("entry_time")
                or ""
            )
            anchor_ts = pd.to_datetime(anchor_raw, errors="coerce")
            df = data.get("df")
            if (
                df is None
                or getattr(df, "empty", True)
                or "timestamp" not in df.columns
                or "high" not in df.columns
                or "low" not in df.columns
                or pd.isna(anchor_ts)
            ):
                return float(current_price)

            ts = pd.to_datetime(df["timestamp"], errors="coerce")
            post = df[ts >= anchor_ts]
            if post.empty:
                return float(current_price)

            if side == "BUY":
                recent_high = float(pd.to_numeric(post["high"], errors="coerce").max())
                return max(float(current_price), recent_high)
            recent_low = float(pd.to_numeric(post["low"], errors="coerce").min())
            return min(float(current_price), recent_low)
        except Exception:
            return float(current_price)

    def _calculate_stepped_sl(self, position_type, entry_price, steps):
        step_percent = self.config['trail_step_percent'] / 100
        if position_type == 'BUY':
            return entry_price * (1 + step_percent * steps)
        return entry_price * (1 - step_percent * steps)

    def _trailing_rule_for_script(self, script_name, risk_percent):
        overrides = self.config.get('trailing_overrides_by_script', {})
        script_rule = overrides.get(script_name, {})

        breakeven_trigger_percent = float(script_rule.get('breakeven_trigger_percent', risk_percent))
        trail_step_percent = float(script_rule.get('trail_step_percent', self.config['trail_step_percent']))

        return breakeven_trigger_percent, trail_step_percent

    def _calculate_stepped_sl_with_percent(self, position_type, entry_price, steps, step_percent):
        step_fraction = step_percent / 100
        if position_type == 'BUY':
            return entry_price * (1 + step_fraction * steps)
        return entry_price * (1 - step_fraction * steps)

    def _profit_lock_ladder_for_script(self, script_name):
        """Return validated/sorted profit-lock ladder rules for a script."""
        script_overrides = self.config.get('profit_lock_ladder_by_script', {})
        raw_ladder = script_overrides.get(script_name, self.config.get('profit_lock_ladder', []))

        ladder = []
        for rule in raw_ladder:
            if not isinstance(rule, dict):
                continue
            try:
                trigger_r = float(rule.get('trigger_r', 0))
                lock_r = float(rule.get('lock_r', 0))
            except (TypeError, ValueError):
                continue

            if trigger_r <= 0 or lock_r <= 0:
                continue
            # Do not lock more than trigger level itself.
            lock_r = min(lock_r, trigger_r)
            ladder.append((trigger_r, lock_r))

        ladder.sort(key=lambda item: item[0])
        return ladder

    def _apply_profit_lock_ladder(
        self,
        script_name,
        position,
        favorable_move,
        risk_percent,
        trigger_basis_percent=None
    ):
        """
        Move SL into profit based on configured R-multiple ladder.
        Example for SELL: at 1.5R reached, lock 0.75R by shifting SL below entry.
        """
        if risk_percent <= 0:
            return False

        ladder = self._profit_lock_ladder_for_script(script_name)
        if not ladder:
            return False

        entry_price = position['entry_price']
        position_type = position['type']
        initial_sl = position.get('initial_sl', position.get('stop_loss', entry_price))
        risk_points = abs(entry_price - initial_sl)
        if risk_points <= 0:
            return False

        basis_percent = float(trigger_basis_percent if trigger_basis_percent and trigger_basis_percent > 0 else risk_percent)
        current_r = favorable_move / basis_percent
        best_rule = None
        for trigger_r, lock_r in ladder:
            if current_r >= trigger_r:
                best_rule = (trigger_r, lock_r)
            else:
                break

        if best_rule is None:
            return False

        trigger_r, lock_r = best_rule
        locked_r = float(position.get('profit_lock_r_locked', 0.0))
        # If we are already at this lock (or tighter), no need to update.
        if lock_r <= locked_r + 1e-9:
            return False

        if position_type == 'BUY':
            lock_sl = entry_price + (lock_r * risk_points)
            new_sl = max(position['stop_loss'], lock_sl)
        else:
            lock_sl = entry_price - (lock_r * risk_points)
            new_sl = min(position['stop_loss'], lock_sl)

        if abs(new_sl - position['stop_loss']) < 1e-9:
            return False

        position['stop_loss'] = new_sl
        position['profit_lock_r_locked'] = lock_r
        position['profit_lock_trigger_r_locked'] = trigger_r
        self._bot_logger.info(
            f"LOCK: {script_name}: Profit-lock rung {trigger_r:.2f}R reached; "
            f"locking {lock_r:.2f}R with SL @ Rs{position['stop_loss']:.2f} "
            f"(favorable move: {favorable_move:.2f}%, current R: {current_r:.2f}, basis: {basis_percent:.2f}%)"
        )
        return True

    def _get_entry_swing_sl(self, df, entry_candle_timestamp, side):
        """Return OB zone SL using BigBeluga Volume Order Blocks logic.

        At EMA crossover:
        BUY  -> lowest low of the last ema_long candles before entry = OB lower boundary = SL
        SELL -> highest high of the last ema_long candles before entry = OB upper boundary = SL

        This matches the BigBeluga 'ta.lowest(length2)' / 'ta.highest(length2)' logic
        where length2 = ema_short + 13 = 18 (same as ema_long).
        """
        if df is None or df.empty or entry_candle_timestamp is None:
            return None

        required_cols = {'timestamp', 'high', 'low'}
        if not required_cols.issubset(df.columns):
            return None

        working = df.sort_values('timestamp').reset_index(drop=True)
        eligible = working[working['timestamp'] <= entry_candle_timestamp]
        if eligible.empty:
            return None

        lookback = int(self.config.get('ema_long', 18))
        lookback_rows = eligible.tail(lookback)
        if lookback_rows.empty:
            return None

        if side == 'BUY':
            return float(lookback_rows['low'].min())
        else:
            return float(lookback_rows['high'].max())

    def _get_entry_order_block_sl(self, df, entry_candle_timestamp, side):
        """Return order-block based SL from 5-minute candles before entry candle.

        BUY  -> low of latest bearish candle (close < open) before entry candle.
        SELL -> high of latest bullish candle (close > open) before entry candle.
        """
        if df is None or df.empty or entry_candle_timestamp is None:
            return None

        required_cols = {'timestamp', 'open', 'close', 'high', 'low'}
        if not required_cols.issubset(df.columns):
            return None

        working = df.sort_values('timestamp').reset_index(drop=True)
        eligible = working[working['timestamp'] <= entry_candle_timestamp]
        if eligible.empty:
            return None

        entry_idx = int(eligible.index[-1])
        prev_idx = entry_idx - 1
        if prev_idx < 0:
            return None

        lookback = max(1, int(self.config.get('order_block_lookback_candles', 12)))
        start_idx = max(0, prev_idx - lookback + 1)

        for idx in range(prev_idx, start_idx - 1, -1):
            row = working.iloc[idx]
            is_bearish = row['close'] < row['open']
            is_bullish = row['close'] > row['open']

            if side == 'BUY' and is_bearish:
                return float(row['low'])
            if side == 'SELL' and is_bullish:
                return float(row['high'])

        return None

    def _update_position_sl(self, script_name, position, current_price):
        """
        Rule:
        - Initial risk = entry to initial SL distance
        - At 1:1 (favorable move >= risk%), SL moves to cost
        - Profit-lock ladder shifts SL into profit at configured R milestones
        - For every extra 0.5% favorable move, SL moves by +0.5% (BUY) / -0.5% (SELL)
        """
        self._ensure_position_fields(position, script_name)
        entry_price = position['entry_price']
        position_type = position['type']

        initial_sl = position.get('initial_sl', position.get('stop_loss', entry_price))
        if entry_price > 0:
            risk_percent = abs((entry_price - initial_sl) / entry_price) * 100
        else:
            risk_percent = 0
        if risk_percent <= 0:
            risk_percent = self.config['trailing_stop_loss_percent']

        breakeven_trigger_percent, step_percent = self._trailing_rule_for_script(script_name, risk_percent)
        # Apply breakeven when either configured % (e.g., 1%) OR 1:1 (risk%) is hit.
        # "Whichever matches first" means we use the lower threshold.
        effective_breakeven_trigger_percent = min(float(breakeven_trigger_percent), float(risk_percent))
        favorable_move = self._favorable_move_percent(position_type, entry_price, current_price)
        quantity = float(position.get('quantity', self._get_order_quantity(script_name)))
        favorable_pnl = self._calculate_realized_pnl(position_type, entry_price, current_price, quantity)

        sl_updated = False

        if favorable_pnl > float(position.get('max_favorable_pnl', 0.0)):
            position['max_favorable_pnl'] = favorable_pnl

        if self._apply_nse_money_lock(script_name, position):
            sl_updated = True

        # If neither money-lock nor % trigger moved us to breakeven yet, wait.
        if favorable_move < effective_breakeven_trigger_percent and not position['breakeven_done']:
            return sl_updated

        if not position['breakeven_done']:
            if position_type == 'BUY':
                position['stop_loss'] = max(position['stop_loss'], entry_price)
            else:
                position['stop_loss'] = min(position['stop_loss'], entry_price)
            position['breakeven_done'] = True
            sl_updated = True
            self._bot_logger.info(f"INFO: {script_name}: 1:1 reached. SL moved to cost @ Rs{entry_price:.2f}")

        # Profit-lock ladder (R-based) runs after breakeven and before stepped trail.
        if self._apply_profit_lock_ladder(
            script_name,
            position,
            favorable_move,
            risk_percent,
            trigger_basis_percent=effective_breakeven_trigger_percent
        ):
            sl_updated = True

        extra_move = max(0.0, favorable_move - effective_breakeven_trigger_percent)
        new_steps = int(extra_move // step_percent)

        if new_steps > position['trail_steps_locked']:
            position['trail_steps_locked'] = new_steps
            stepped_sl = self._calculate_stepped_sl_with_percent(position_type, entry_price, new_steps, step_percent)

            if position_type == 'BUY':
                position['stop_loss'] = max(position['stop_loss'], stepped_sl)
            else:
                position['stop_loss'] = min(position['stop_loss'], stepped_sl)

            sl_updated = True

            self._bot_logger.info(
                f"UPDATE: {script_name}: Trailing SL updated to Rs{position['stop_loss']:.2f} "
                f"(favorable move: {favorable_move:.2f}%, steps: {new_steps})"
            )

        return sl_updated

    def _apply_nse_money_lock(self, script_name, position):
        cfg = self.config.get('nse_money_lock', {}) or {}
        if not bool(cfg.get('enabled', False)):
            return False

        scripts = cfg.get('scripts') or self.config.get('segment_scripts', {}).get('NSE', [])
        if script_name not in scripts:
            return False

        trigger_pnl = float(cfg.get('trigger_pnl', 5000.0))
        step_pnl = float(cfg.get('step_pnl', 500.0))
        lock_increment = float(cfg.get('lock_increment_pnl', 500.0))
        if trigger_pnl <= 0 or step_pnl <= 0 or lock_increment <= 0:
            return False

        max_favorable_pnl = float(position.get('max_favorable_pnl', 0.0))
        if max_favorable_pnl < trigger_pnl:
            return False

        rung = int((max_favorable_pnl - trigger_pnl) // step_pnl) + 1
        if rung <= 0:
            return False

        target_lock_pnl = rung * lock_increment
        prev_locked_pnl = float(position.get('money_lock_pnl_locked', 0.0))
        if target_lock_pnl <= prev_locked_pnl:
            return False

        qty = float(position.get('quantity', self._get_order_quantity(script_name)))
        if qty <= 0:
            return False

        entry = float(position.get('entry_price', 0.0))
        side = str(position.get('type', '')).upper()
        if side == 'BUY':
            lock_sl = entry + (target_lock_pnl / qty)
            new_sl = max(float(position.get('stop_loss', entry)), lock_sl)
        elif side == 'SELL':
            lock_sl = entry - (target_lock_pnl / qty)
            new_sl = min(float(position.get('stop_loss', entry)), lock_sl)
        else:
            return False

        if abs(new_sl - float(position.get('stop_loss', entry))) < 1e-9:
            return False

        position['stop_loss'] = new_sl
        position['money_lock_steps_locked'] = max(int(position.get('money_lock_steps_locked', 0)), rung)
        position['money_lock_pnl_locked'] = target_lock_pnl
        position['breakeven_done'] = True
        self._bot_logger.info(
            f"MONEY-LOCK: {script_name} rung={rung} | max_favorable_pnl={max_favorable_pnl:.2f} | "
            f"locked_pnl={target_lock_pnl:.2f} | SL @ Rs{new_sl:.2f}"
        )
        return True

    def _resample_for_signal(self, df):
        """Resample API candles to strategy timeframe for signal generation."""
        signal_interval = self.config.get('signal_interval', '1minute')
        if signal_interval == '1minute':
            return df

        if signal_interval == '5minute':
            resampled = (
                df.set_index('timestamp')
                .sort_index()
                .resample('5min')
                .agg({
                    'open': 'first',
                    'high': 'max',
                    'low': 'min',
                    'close': 'last',
                    'volume': 'sum',
                    'oi': 'last'
                })
                .dropna(subset=['open', 'high', 'low', 'close'])
                .reset_index()
            )
            return resampled

        return df

    def _get_last_closed_candle_row(self, df):
        """Return last fully closed signal candle row based on configured signal interval."""
        if df is None or df.empty:
            return None

        signal_interval = self.config.get('signal_interval', '1minute')
        if signal_interval != '5minute':
            return df.iloc[-1]

        latest_ts = df['timestamp'].iloc[-1]
        if getattr(latest_ts, 'tzinfo', None) is not None:
            now_ts = pd.Timestamp.now(tz=latest_ts.tzinfo)
        else:
            now_ts = pd.Timestamp.now()

        current_bucket_start = now_ts.floor('5min')
        last_closed_bucket_start = current_bucket_start - pd.Timedelta(minutes=5)

        closed_df = df[df['timestamp'] <= last_closed_bucket_start]
        if closed_df.empty:
            return None
        return closed_df.iloc[-1]

    def _market_data_is_kite(self) -> bool:
        return (
            str(self.config.get("market_data_provider") or "upstox").strip().lower() == "kite"
        )

    def _signal_bucket_minutes(self) -> int:
        """Minutes per bar for ``signal_interval`` (used for boundary wake timing)."""
        s = str(self.config.get("signal_interval") or "5minute").strip().lower().replace(" ", "")
        if s in ("5minute", "5m", "5min"):
            return 5
        if s in ("1minute", "1m", "1min"):
            return 1
        if s in ("3minute", "3m", "3min"):
            return 3
        if s in ("15minute", "15m", "15min"):
            return 15
        if s in ("30minute", "30m", "30min"):
            return 30
        if s in ("60minute", "60m", "1hour", "1h"):
            return 60
        return 5

    def _kite_signal_boundary_wake_enabled(self) -> bool:
        """Wake the main loop right after each signal bar closes (IST), not only on loop_interval."""
        if not self._market_data_is_kite():
            return False
        return os.environ.get("KITE_SIGNAL_BOUNDARY_WAKE", "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )

    def _seconds_until_next_signal_bar_fire_ist(self) -> float:
        """Seconds until (floor IST bucket + offset) for the next strategy evaluation after a bar close."""
        try:
            offset = float(os.environ.get("KITE_BOUNDARY_EVAL_OFFSET_SEC", "2"))
        except ValueError:
            offset = 2.0
        offset = max(0.0, offset)
        mins = max(1, int(self._signal_bucket_minutes()))
        ts = pd.Timestamp(datetime.now(ZoneInfo("Asia/Kolkata")))
        flo = ts.floor(f"{mins}min")
        fire = flo + pd.Timedelta(seconds=offset)
        if ts < fire:
            return max(0.05, (fire - ts).total_seconds())
        nxt = flo + pd.Timedelta(minutes=mins) + pd.Timedelta(seconds=offset)
        return max(0.05, (nxt - ts).total_seconds())

    def _kite_stream_drive_exits(self) -> bool:
        """When True, SL/target checks run on Kite LTP ticks (not only on the strategy loop)."""
        if not self._market_data_is_kite():
            return False
        return os.environ.get("KITE_STREAM_DRIVE_EXITS", "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )

    def _on_kite_stream_after_ticks(self) -> None:
        """KiteTicker thread: throttle and run SL/target path without waiting for loop_interval."""
        if not self.running:
            return
        if not self._kite_stream_drive_exits():
            return
        now_m = time.monotonic()
        if now_m - self._last_stream_exit_mono < self._stream_exit_min_interval:
            return
        self._last_stream_exit_mono = now_m
        if not self.positions and not self.paper_positions:
            return
        if not self._strategy_lock.acquire(blocking=False):
            return
        try:
            self._do_stream_exit_pass()
        finally:
            self._strategy_lock.release()

    def _do_stream_exit_pass(self) -> None:
        """Merge live LTP into cached candle snapshot; fire SL/target exits via Upstox immediately."""
        if self._kite_tick_stream is None:
            return
        script_data: list[dict] = []
        seen: set[str] = set()
        for script in list(self.positions.keys()) + list(self.paper_positions.keys()):
            s = str(script or "").strip().upper()
            if not s or s in seen:
                continue
            seen.add(s)
            cache = self._script_data_cache.get(s)
            if not cache:
                continue
            px = self._kite_tick_stream.last_price(s)
            if px is None or px <= 0:
                continue
            px = float(px)
            row = dict(cache)
            row["current_price"] = px
            ch = float(cache.get("current_high", px))
            cl = float(cache.get("current_low", px))
            row["current_high"] = max(ch, px)
            row["current_low"] = min(cl, px)
            row["instrument_key"] = cache.get("instrument_key")
            script_data.append(row)
        if not script_data:
            return
        self.execute_trading_logic(
            script_data,
            allow_new_entries=False,
            now_ist=self._now_ist(),
            stream_exit_only=True,
        )

    def _get_kite_credentials(self) -> tuple[str, str] | None:
        z = load_zerodha_credentials_for_user(self.username)
        api_key = (z.get("api_key") or "").strip()
        access_token = (z.get("access_token") or "").strip()
        if not api_key or not access_token:
            return None
        return api_key, access_token

    def _resolve_kite_futures_token(self, script_name: str) -> int | None:
        sn = str(script_name or "").strip().upper()
        ov = (self.config.get("kite_instrument_token_overrides") or {}).get(sn)
        if ov is not None:
            try:
                t = int(ov)
                if t > 0:
                    self._kite_script_tokens[sn] = t
                    return t
            except (TypeError, ValueError):
                pass
        if sn in self._kite_script_tokens:
            return int(self._kite_script_tokens[sn])
        creds = self._get_kite_credentials()
        if not creds:
            return None
        api_key, access_token = creds
        tok = resolve_kite_instrument_token(sn, api_key, access_token)
        if tok and tok > 0:
            self._kite_script_tokens[sn] = int(tok)
            return int(tok)
        return None

    def _ensure_kite_tick_feed(self, script_names: list[str]) -> None:
        """Subscribe Kite WebSocket LTP for the given scripts (idempotent)."""
        if not self._market_data_is_kite():
            return
        creds = self._get_kite_credentials()
        if not creds:
            self._bot_logger.warning(
                "KITE: missing api_key/access_token in zerodha_credentials.json — tick stream disabled"
            )
            return
        api_key, access_token = creds
        mp: dict[str, int] = {}
        for sn in script_names:
            s = str(sn or "").strip().upper()
            if not s:
                continue
            tok = self._resolve_kite_futures_token(s)
            if tok and tok > 0:
                mp[s] = int(tok)
        if not mp:
            return
        if self._kite_tick_stream is None:
            self._kite_tick_stream = KiteTickStream(api_key, access_token, self._bot_logger)
            self._kite_tick_stream.set_subscriptions(mp)
            if self._kite_tick_stream.start():
                self._bot_logger.info(
                    "KITE TICK STREAM: started for %s", ",".join(sorted(mp.keys()))
                )
        else:
            self._kite_tick_stream.set_subscriptions(mp)
        if self._kite_tick_stream is not None:
            self._kite_tick_stream.on_after_ticks = (
                self._on_kite_stream_after_ticks if self._kite_stream_drive_exits() else None
            )

    def _fetch_market_data_kite(self, script_name: str, instrument_key: str):
        """Candles from Kite REST (continuous futures where applicable); LTP from tick stream in process_script."""
        creds = self._get_kite_credentials()
        if not creds:
            self._bot_logger.error(
                "KITE: no Zerodha credentials for user %s — cannot load candles", self.username
            )
            return None
        api_key, access_token = creds
        tok = self._resolve_kite_futures_token(script_name)
        if not tok:
            self._bot_logger.error(
                "KITE: could not resolve instrument_token for %s (EQ/FUT) — set kite_instrument_token_overrides",
                script_name,
            )
            return None
        kite_iv = map_bot_interval_to_kite(self.config.get("interval", "1minute"))
        from_d, to_d = default_swing_window()
        use_cont = "1" if script_name in _INDEX_FO_SCRIPT_NAMES else "0"
        try:
            rows = fetch_historical_raw(
                api_key,
                access_token,
                tok,
                kite_iv,
                from_d,
                to_d,
                continuous=use_cont,
            )
        except Exception as e:
            self._bot_logger.warning(
                "KITE: historical %s continuous=%s failed (%s); retry continuous=0",
                script_name,
                use_cont,
                e,
            )
            try:
                rows = fetch_historical_raw(
                    api_key,
                    access_token,
                    tok,
                    kite_iv,
                    from_d,
                    to_d,
                    continuous="0",
                )
            except Exception as e2:
                self._bot_logger.error("KITE: historical %s failed: %s", script_name, e2)
                return None
        df = kite_candles_to_dataframe(rows)
        if df is None or df.empty:
            return None
        df = self._resample_for_signal(df)
        if df is None or df.empty:
            return None
        self._bot_logger.info(
            " %s: Kite %d candles (%s) | Latest close Rs%.2f",
            script_name,
            len(df),
            self.config.get("signal_interval", "1minute"),
            float(df["close"].iloc[-1]),
        )
        return df

    def fetch_market_data(self, script_name, instrument_key):
        """Fetch and combine historical + intraday market data"""
        if self._market_data_is_kite():
            return self._fetch_market_data_kite(script_name, instrument_key)
        try:
            instrument_key = self.config.get('scripts', {}).get(script_name, instrument_key)
            # Get dates
            to_date = datetime.now().strftime('%Y-%m-%d')
            from_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
            data_interval = self.config.get('interval', '1minute')
            
            # Fetch historical data
            df_hist = self.client.get_historical_candles(
                instrument_key, 
                data_interval, 
                from_date, 
                to_date
            )
            
            # Fetch intraday data
            df_intraday = self.client.get_intraday_candles(
                instrument_key, 
                data_interval
            )

            if (
                df_hist is None
                and df_intraday is None
                and self._should_attempt_contract_roll(script_name)
            ):
                next_key = self._switch_to_next_contract(script_name, instrument_key)
                if next_key != instrument_key:
                    self._bot_logger.info(
                        f"RETRY: {script_name} refetching market data with rolled contract {next_key}"
                    )
                    instrument_key = next_key
                    df_hist = self.client.get_historical_candles(
                        instrument_key,
                        data_interval,
                        from_date,
                        to_date
                    )
                    df_intraday = self.client.get_intraday_candles(
                        instrument_key,
                        data_interval
                    )
            
            # Combine data
            if df_hist is not None and df_intraday is not None:
                df = pd.concat([df_hist, df_intraday], ignore_index=True)
                df = df.drop_duplicates(subset=['timestamp'], keep='last')
                df = df.sort_values('timestamp').reset_index(drop=True)
            elif df_hist is not None:
                df = df_hist
            elif df_intraday is not None:
                df = df_intraday
            else:
                return None

            df = self._resample_for_signal(df)
            if df is None or df.empty:
                return None
            
            self._bot_logger.info(
                f" {script_name}: {len(df)} candles ({self.config.get('signal_interval', '1minute')}) "
                f"| Latest: Rs{df['close'].iloc[-1]:.2f}"
            )
            return df
            
        except Exception as e:
            self._bot_logger.error(f"ERROR: Error fetching data for {script_name}: {e}")
            return None
    
    def process_script(self, script_name, instrument_key):
        """Process a single script for trading signals"""
        try:
            # Fetch market data
            df = self.fetch_market_data(script_name, instrument_key)
            if df is None or len(df) < self.config['ema_long']:
                self._bot_logger.warning(f"WARNING: Insufficient data for {script_name}")
                return None
            
            # Calculate technical indicators
            df = self.analyzer.calculate_signals(
                df, 
                self.config['ema_short'], 
                self.config['ema_long']
            )
            
            if df is None:
                return None

            adx_period = int(self.config.get('adx_period', 14))
            adx_series, plus_di_series, minus_di_series = self._calculate_adx_values(df, adx_period)
            
            # Get latest values
            latest = df.iloc[-1]
            current_price = latest['close']
            signal = latest['signal']
            ema_short = latest['ema_short']
            ema_long = latest['ema_long']
            crossover = latest['crossover']

            # Last fully closed candle values (used for strict entry)
            closed_row = self._get_last_closed_candle_row(df)
            if closed_row is not None:
                closed_signal = closed_row['signal']
                closed_crossover = closed_row['crossover']
                closed_ema_short = closed_row['ema_short']
                closed_ema_long = closed_row['ema_long']
                closed_timestamp = closed_row['timestamp']
                # EMA18 of the candle immediately before closed_row (for slope check)
                closed_row_idx = df.index[df['timestamp'] == closed_row['timestamp']]
                if len(closed_row_idx) > 0 and closed_row_idx[0] > 0:
                    prev_row = df.iloc[closed_row_idx[0] - 1]
                    closed_ema_long_prev = float(prev_row['ema_long'])
                else:
                    closed_ema_long_prev = float(closed_ema_long)
                if len(closed_row_idx) > 0:
                    idx = int(closed_row_idx[0])
                    closed_adx = float(adx_series.iloc[idx])
                    closed_plus_di = float(plus_di_series.iloc[idx])
                    closed_minus_di = float(minus_di_series.iloc[idx])
                else:
                    closed_adx = float(adx_series.iloc[-1])
                    closed_plus_di = float(plus_di_series.iloc[-1])
                    closed_minus_di = float(minus_di_series.iloc[-1])
            else:
                closed_signal = signal
                closed_crossover = False
                closed_ema_short = ema_short
                closed_ema_long = ema_long
                closed_timestamp = None
                closed_ema_long_prev = float(ema_long)
                closed_adx = float(adx_series.iloc[-1])
                closed_plus_di = float(plus_di_series.iloc[-1])
                closed_minus_di = float(minus_di_series.iloc[-1])
            
            # Determine signal status
            if signal == 1:
                signal_status = "BUY"
                color = Fore.GREEN
            elif signal == -1:
                signal_status = "SELL"
                color = Fore.RED
            else:
                signal_status = "NEUTRAL"
                color = Fore.YELLOW
            
            out = {
                'script_name': script_name,
                'instrument_key': self.config.get('scripts', {}).get(script_name, instrument_key),
                'current_price': current_price,
                'current_high': float(latest.get('high', current_price)),
                'current_low': float(latest.get('low', current_price)),
                'signal': signal,
                'signal_status': signal_status,
                'color': color,
                'ema_short': ema_short,
                'ema_long': ema_long,
                'crossover': crossover,
                'latest_timestamp': latest['timestamp'],
                'entry_signal': closed_signal,
                'entry_crossover': closed_crossover,
                'entry_ema_short': closed_ema_short,
                'entry_ema_long': closed_ema_long,
                'entry_ema_long_prev': closed_ema_long_prev,
                'entry_adx': closed_adx,
                'entry_plus_di': closed_plus_di,
                'entry_minus_di': closed_minus_di,
                'entry_candle_timestamp': closed_timestamp,
                'df': df
            }
            if self._market_data_is_kite() and self._kite_tick_stream is not None:
                klp = self._kite_tick_stream.last_price(script_name)
                if klp is not None and klp > 0:
                    out["current_price"] = float(klp)
            return out
            
        except Exception as e:
            self._bot_logger.error(f"ERROR: Error processing {script_name}: {e}")
            return None
    
    def print_status_table(self, script_data):
        """Print formatted status table"""
        print("\n" + "="*110)
        print(f"{Fore.CYAN}{'SCRIPT':<15} {'PRICE':<12} {'EMA'+str(self.config['ema_short']):<12} {'EMA'+str(self.config['ema_long']):<12} {'SIGNAL':<12} {'STATUS':<20}{Style.RESET_ALL}")
        print("="*110)
        
        for data in script_data:
            if data:
                crossover_text = f"{Fore.YELLOW}[CROSSOVER]{Style.RESET_ALL}" if data['crossover'] else ""
                print(f"{data['script_name']:<15} "
                      f"Rs{data['current_price']:<10.2f} "
                      f"{data['ema_short']:<12.2f} "
                      f"{data['ema_long']:<12.2f} "
                      f"{data['color']}{data['signal_status']:<12}{Style.RESET_ALL} "
                      f"{crossover_text:<20}")
                script_name = data['script_name']
                signal_timestamp = data.get('latest_timestamp')
                if hasattr(signal_timestamp, 'isoformat'):
                    signal_time_text = signal_timestamp.isoformat()
                else:
                    signal_time_text = datetime.now().isoformat()

                self._market_status_logger.info(
                    f"{script_name} | EMA={data['ema_short']:.2f}/{data['ema_long']:.2f} | "
                    f"Status={data['signal_status']} | Timestamp={signal_time_text}"
                )
        
        print("="*110)
        print(f"{Fore.YELLOW}Total P&L: Rs{self.total_pnl:.2f}{Style.RESET_ALL}")
        print(
            f"{Fore.YELLOW}Live positions: {len(self.positions)} · "
            f"Paper: {len(self.paper_positions)} · "
            f"Paper P&L: Rs{self.paper_total_pnl:.2f}{Style.RESET_ALL}"
        )
        if self.positions:
            for script, pos in self.positions.items():
                sl = pos.get('stop_loss', pos.get('entry_price', 0))
                print(f"   - {script}: {pos['type']} @ Rs{pos['entry_price']:.2f} | SL: Rs{sl:.2f}")
        if self.paper_positions:
            for script, pos in self.paper_positions.items():
                sl = pos.get('stop_loss', pos.get('entry_price', 0))
                print(
                    f"   - {script} [PAPER]: {pos['type']} @ Rs{pos['entry_price']:.2f} | SL: Rs{sl:.2f}"
                )
        print("="*110 + "\n")
    
    def execute_trading_logic(
        self,
        script_data,
        allow_new_entries=True,
        now_ist=None,
        stream_exit_only: bool = False,
    ):
        """Execute trading logic based on signals.

        When ``stream_exit_only`` is True (Kite LTP path), only SL/target/trailing and
        last_polled_price updates run; OB/crossover exits and new entries are skipped.
        """
        if now_ist is None:
            now_ist = self._now_ist()

        for data in script_data:
            if not data:
                continue
            
            script_name = data['script_name']
            if stream_exit_only and script_name not in self.positions and script_name not in self.paper_positions:
                continue
            signal = data['signal']
            current_price = data['current_price']
            current_high = float(data.get('current_high', current_price) or current_price)
            current_low = float(data.get('current_low', current_price) or current_price)
            crossover = data['crossover']
            instrument_key = data['instrument_key']
            confirmed_signal = data.get('entry_signal', signal)
            confirmed_crossover = data.get('entry_crossover', crossover)
            confirmed_candle_timestamp = data.get('entry_candle_timestamp')
            
            # Check if we have an open position (live broker or paper)
            position = None
            is_paper = False
            if script_name in self.positions:
                position = self.positions[script_name]
                is_paper = False
            elif script_name in self.paper_positions:
                position = self.paper_positions[script_name]
                is_paper = True

            if position is not None:
                self._ensure_position_fields(position, script_name)
                if position.get('chart_percent') is None:
                    chart_backfill = self._backfill_chart_percent(
                        script_name,
                        position,
                        data.get('df')
                    )
                    if chart_backfill is not None:
                        position['chart_percent'] = chart_backfill
                        self.save_state()

                confirmed_time_text = confirmed_candle_timestamp.isoformat() if hasattr(confirmed_candle_timestamp, 'isoformat') else 'NA'
                last_eval_ts = self.last_position_eval_logged.get(script_name)
                if (
                    not stream_exit_only
                    and confirmed_time_text != 'NA'
                    and last_eval_ts != confirmed_time_text
                ):
                    self._bot_logger.info(
                        f"VERIFY: {script_name} open={position['type']} | entry={position['entry_price']:.2f} | "
                        f"closed_signal={confirmed_signal} | closed_crossover={bool(confirmed_crossover)} | "
                        f"closed_time={confirmed_time_text}"
                    )
                    self.last_position_eval_logged[script_name] = confirmed_time_text

                # Update stepped trailing SL as per strategy using post-entry range only.
                # This ensures trailing updates are based on highs/lows formed after entry.
                trail_ref_price = self._post_entry_trailing_reference_price(
                    position,
                    data,
                    current_price,
                )
                if bool(position.get("manual_execution")):
                    # Keep intraloop sampled range for manual trades too (10s loop),
                    # while still respecting post-entry anchor from helper.
                    if position.get("type") == "BUY":
                        trail_ref_price = max(float(trail_ref_price), float(current_high))
                    else:
                        trail_ref_price = min(float(trail_ref_price), float(current_low))
                sl_updated = self._update_position_sl(script_name, position, trail_ref_price)
                if sl_updated:
                    if bool(position.get("manual_execution")) and telegram_notifications_enabled_for_user(self.username):
                        exit_side = "SELL" if position["type"] == "BUY" else "BUY"
                        _ = send_trade_notification(
                            {
                                "account": self.username,
                                "symbol": script_name,
                                "action": exit_side,
                                "quantity": float(position.get("quantity", self._get_order_quantity(script_name))),
                                "price": float(current_price),
                                "reason": "TRAILING_SL_UPDATED",
                                "stop_loss": float(position.get("stop_loss", current_price)),
                                "target_price": float(position.get("target_price", current_price)),
                                "note": "Manual trade - keep broker SL in sync",
                                "timestamp": self._now_ist(),
                            }
                        )
                    self.save_state()

                # Stop loss check
                stop_loss = position['stop_loss']
                prev_polled_price_raw = position.get('last_polled_price')
                prev_polled_price = (
                    float(prev_polled_price_raw)
                    if prev_polled_price_raw is not None
                    else float(current_price)
                )
                # If SL was updated in this loop, start a fresh 10s gap baseline now.
                # This prevents retroactive SL hits against a tighter SL using older prev_poll.
                if sl_updated:
                    prev_polled_price = float(current_price)
                # Polling-based exits: only act on current observed price at each 10s loop.
                # This avoids retroactive exits based on full candle high/low extremes.
                # We detect threshold touch within the observed 10s gap [prev_poll, current_poll].
                if position['type'] == 'BUY' and (
                    current_price <= stop_loss
                    or (prev_polled_price > stop_loss and current_price <= stop_loss)
                ):
                    sl_reason = self._stoploss_reason(position)
                    self._bot_logger.warning(
                        f"ALERT: SL hit for {script_name}. Closing BUY @ Rs{current_price:.2f} "
                        f"(SL: Rs{stop_loss:.2f}, prev_poll: Rs{prev_polled_price:.2f}, current: Rs{current_price:.2f})"
                    )
                    if bool(position.get("manual_execution")):
                        self._notify_manual_close_needed(
                            script_name, position, "SELL", current_price, sl_reason
                        )
                        if is_paper:
                            del self.paper_positions[script_name]
                        else:
                            self._close_all_option_for_script(script_name, "FUTURES_EXIT")
                            del self.positions[script_name]
                        self.save_state()
                        continue
                    if is_paper:
                        self._paper_exit_after_signal(
                            script_name,
                            position,
                            "SELL",
                            current_price,
                            sl_reason,
                            extra_log=(
                                f"sl={stop_loss:.2f}; entry_adx={float(position.get('signal_adx', 0.0) or 0.0):.2f}; "
                                f"plus_di={float(position.get('signal_plus_di', 0.0) or 0.0):.2f}; "
                                f"minus_di={float(position.get('signal_minus_di', 0.0) or 0.0):.2f}"
                            ),
                        )
                        continue
                    success, order_result = self._place_order_with_result(
                        script_name,
                        "SELL",
                        current_price,
                        sl_reason,
                        realized_pnl=self._calculate_realized_pnl(
                            position['type'],
                            float(position['entry_price']),
                            float(current_price),
                            float(position.get('quantity', self._get_order_quantity(script_name)))
                        ),
                        entry_adx=float(position.get('signal_adx', 0.0) or 0.0),
                        entry_plus_di=float(position.get('signal_plus_di', 0.0) or 0.0),
                        entry_minus_di=float(position.get('signal_minus_di', 0.0) or 0.0),
                    )
                    if not success:
                        continue
                    order_id = (order_result.get('data') or {}).get('order_id', 'NA')
                    self._log_order_event(
                        script_name,
                        action="EXIT",
                        side="SELL",
                        price=current_price,
                        reason=sl_reason,
                        extra=(
                            f"entry={position['entry_price']:.2f}; sl={stop_loss:.2f}; order_id={order_id}; "
                            f"entry_adx={float(position.get('signal_adx', 0.0) or 0.0):.2f}; "
                            f"plus_di={float(position.get('signal_plus_di', 0.0) or 0.0):.2f}; "
                            f"minus_di={float(position.get('signal_minus_di', 0.0) or 0.0):.2f}"
                        )
                    )
                    self._notify_dashboard_trade_close(script_name, position, current_price)
                    self._close_all_option_for_script(script_name, "FUTURES_EXIT")
                    del self.positions[script_name]
                    self.save_state()
                    continue

                if position['type'] == 'SELL' and (
                    current_price >= stop_loss
                    or (prev_polled_price < stop_loss and current_price >= stop_loss)
                ):
                    sl_reason = self._stoploss_reason(position)
                    self._bot_logger.warning(
                        f"ALERT: SL hit for {script_name}. Closing SELL @ Rs{current_price:.2f} "
                        f"(SL: Rs{stop_loss:.2f}, prev_poll: Rs{prev_polled_price:.2f}, current: Rs{current_price:.2f})"
                    )
                    if bool(position.get("manual_execution")):
                        self._notify_manual_close_needed(
                            script_name, position, "BUY", current_price, sl_reason
                        )
                        if is_paper:
                            del self.paper_positions[script_name]
                        else:
                            self._close_all_option_for_script(script_name, "FUTURES_EXIT")
                            del self.positions[script_name]
                        self.save_state()
                        continue
                    if is_paper:
                        self._paper_exit_after_signal(
                            script_name,
                            position,
                            "BUY",
                            current_price,
                            sl_reason,
                            extra_log=(
                                f"sl={stop_loss:.2f}; entry_adx={float(position.get('signal_adx', 0.0) or 0.0):.2f}; "
                                f"plus_di={float(position.get('signal_plus_di', 0.0) or 0.0):.2f}; "
                                f"minus_di={float(position.get('signal_minus_di', 0.0) or 0.0):.2f}"
                            ),
                        )
                        continue
                    success, order_result = self._place_order_with_result(
                        script_name,
                        "BUY",
                        current_price,
                        sl_reason,
                        realized_pnl=self._calculate_realized_pnl(
                            position['type'],
                            float(position['entry_price']),
                            float(current_price),
                            float(position.get('quantity', self._get_order_quantity(script_name)))
                        ),
                        entry_adx=float(position.get('signal_adx', 0.0) or 0.0),
                        entry_plus_di=float(position.get('signal_plus_di', 0.0) or 0.0),
                        entry_minus_di=float(position.get('signal_minus_di', 0.0) or 0.0),
                    )
                    if not success:
                        continue
                    order_id = (order_result.get('data') or {}).get('order_id', 'NA')
                    self._log_order_event(
                        script_name,
                        action="EXIT",
                        side="BUY",
                        price=current_price,
                        reason=sl_reason,
                        extra=(
                            f"entry={position['entry_price']:.2f}; sl={stop_loss:.2f}; order_id={order_id}; "
                            f"entry_adx={float(position.get('signal_adx', 0.0) or 0.0):.2f}; "
                            f"plus_di={float(position.get('signal_plus_di', 0.0) or 0.0):.2f}; "
                            f"minus_di={float(position.get('signal_minus_di', 0.0) or 0.0):.2f}"
                        )
                    )
                    self._notify_dashboard_trade_close(script_name, position, current_price)
                    self._close_all_option_for_script(script_name, "FUTURES_EXIT")
                    del self.positions[script_name]
                    self.save_state()
                    continue

                # Target check
                favorable_move = self._favorable_move_percent(position['type'], position['entry_price'], current_price)
                target_price = float(position.get('target_price', position['entry_price']))
                target_hit = (
                    (
                        position['type'] == 'BUY'
                        and (
                            current_price >= target_price
                            or (prev_polled_price < target_price and current_price >= target_price)
                        )
                    ) or
                    (
                        position['type'] == 'SELL'
                        and (
                            current_price <= target_price
                            or (prev_polled_price > target_price and current_price <= target_price)
                        )
                    )
                )
                if target_hit:
                    self._bot_logger.info(
                        f" Target hit for {script_name}. Closing {position['type']} @ Rs{current_price:.2f} "
                        f"(target: Rs{target_price:.2f}, prev_poll: Rs{prev_polled_price:.2f}, current: Rs{current_price:.2f}, move: {favorable_move:.2f}%)"
                    )
                    exit_side = "SELL" if position['type'] == 'BUY' else "BUY"
                    if bool(position.get("manual_execution")):
                        self._notify_manual_close_needed(
                            script_name, position, exit_side, current_price, "TARGET_HIT"
                        )
                        if is_paper:
                            del self.paper_positions[script_name]
                        else:
                            self._close_all_option_for_script(script_name, "FUTURES_EXIT")
                            del self.positions[script_name]
                        self.save_state()
                        continue
                    if is_paper:
                        self._paper_exit_after_signal(
                            script_name,
                            position,
                            exit_side,
                            current_price,
                            "TARGET_HIT",
                            extra_log=(
                                f"target={position.get('target_price', 0):.2f}; "
                                f"entry_adx={float(position.get('signal_adx', 0.0) or 0.0):.2f}; "
                                f"plus_di={float(position.get('signal_plus_di', 0.0) or 0.0):.2f}; "
                                f"minus_di={float(position.get('signal_minus_di', 0.0) or 0.0):.2f}"
                            ),
                        )
                        continue
                    success, order_result = self._place_order_with_result(
                        script_name,
                        exit_side,
                        current_price,
                        "TARGET_HIT",
                        realized_pnl=self._calculate_realized_pnl(
                            position['type'],
                            float(position['entry_price']),
                            float(current_price),
                            float(position.get('quantity', self._get_order_quantity(script_name)))
                        ),
                        entry_adx=float(position.get('signal_adx', 0.0) or 0.0),
                        entry_plus_di=float(position.get('signal_plus_di', 0.0) or 0.0),
                        entry_minus_di=float(position.get('signal_minus_di', 0.0) or 0.0),
                    )
                    if not success:
                        continue
                    order_id = (order_result.get('data') or {}).get('order_id', 'NA')
                    self._log_order_event(
                        script_name,
                        action="EXIT",
                        side=exit_side,
                        price=current_price,
                        reason="TARGET_HIT",
                        extra=(
                            f"entry={position['entry_price']:.2f}; target={position.get('target_price', 0):.2f}; order_id={order_id}; "
                            f"entry_adx={float(position.get('signal_adx', 0.0) or 0.0):.2f}; "
                            f"plus_di={float(position.get('signal_plus_di', 0.0) or 0.0):.2f}; "
                            f"minus_di={float(position.get('signal_minus_di', 0.0) or 0.0):.2f}"
                        )
                    )
                    self._notify_dashboard_trade_close(script_name, position, current_price)
                    self._close_all_option_for_script(script_name, "FUTURES_EXIT")
                    del self.positions[script_name]
                    self.save_state()
                    continue

                # Carry forward the current observation as baseline for next gap check (loop or LTP stream).
                position['last_polled_price'] = float(current_price)

                if stream_exit_only:
                    continue

                # Exit on OB zone breach (BigBeluga logic) or on confirmed crossover (reversal) with OB present
                ob_zone_boundary = position.get('initial_sl')
                last_closed_candle = self._get_last_closed_candle_row(data.get('df'))
                crossover_exit = False
                ob_breached = False
                if ob_zone_boundary is not None and last_closed_candle is not None:
                    candle_close = float(last_closed_candle['close'])
                    candle_ts = last_closed_candle['timestamp']
                    candle_ts_str = candle_ts.isoformat() if hasattr(candle_ts, 'isoformat') else str(candle_ts)
                    ob_breached = (
                        (position['type'] == 'BUY' and candle_close < ob_zone_boundary) or
                        (position['type'] == 'SELL' and candle_close > ob_zone_boundary)
                    )
                    # Crossover exit: always exit on confirmed crossover with OB present
                    if (
                        (position['type'] == 'BUY' and confirmed_signal == -1 and confirmed_crossover) or
                        (position['type'] == 'SELL' and confirmed_signal == 1 and confirmed_crossover)
                    ):
                        crossover_exit = True

                if ob_breached or crossover_exit:
                    reason = "OB_ZONE_BREACH" if ob_breached else "OPPOSITE_CROSSOVER"
                    self._bot_logger.info(
                        f"EXIT: {reason} for {script_name}. Closing {position['type']} @ Rs{current_price:.2f} "
                        f"(candle_close={candle_close:.2f} vs ob_boundary={ob_zone_boundary:.2f}, candle={candle_ts_str})"
                    )
                    exit_side = "SELL" if position['type'] == 'BUY' else "BUY"
                    if bool(position.get("manual_execution")):
                        self._notify_manual_close_needed(
                            script_name, position, exit_side, current_price, reason
                        )
                        if is_paper:
                            del self.paper_positions[script_name]
                        else:
                            del self.positions[script_name]
                        self.save_state()
                        continue
                    if is_paper:
                        self._paper_exit_after_signal(
                            script_name,
                            position,
                            exit_side,
                            current_price,
                            reason,
                            extra_log=(
                                f"signal_time={confirmed_time_text}; "
                                f"entry_adx={float(position.get('signal_adx', 0.0) or 0.0):.2f}; "
                                f"plus_di={float(position.get('signal_plus_di', 0.0) or 0.0):.2f}; "
                                f"minus_di={float(position.get('signal_minus_di', 0.0) or 0.0):.2f}"
                            ),
                        )
                        if crossover_exit:
                            reversal_signal = -1 if position['type'] == 'BUY' else 1
                            reversal_crossover = True
                            data['entry_signal'] = reversal_signal
                            data['entry_crossover'] = reversal_crossover
                            data['entry_candle_timestamp'] = last_closed_candle['timestamp']
                            self.process_script(script_name, data['instrument_key'])
                        continue
                    success, order_result = self._place_order_with_result(
                        script_name,
                        exit_side,
                        current_price,
                        reason,
                        realized_pnl=self._calculate_realized_pnl(
                            position['type'],
                            float(position['entry_price']),
                            float(current_price),
                            float(position.get('quantity', self._get_order_quantity(script_name)))
                        ),
                        entry_adx=float(position.get('signal_adx', 0.0) or 0.0),
                        entry_plus_di=float(position.get('signal_plus_di', 0.0) or 0.0),
                        entry_minus_di=float(position.get('signal_minus_di', 0.0) or 0.0),
                    )
                    if not success:
                        continue
                    order_id = (order_result.get('data') or {}).get('order_id', 'NA')
                    self._log_order_event(
                        script_name,
                        action="EXIT",
                        side=exit_side,
                        price=current_price,
                        reason=reason,
                        extra=(
                            f"signal_time={confirmed_time_text}; order_id={order_id}; "
                            f"entry_adx={float(position.get('signal_adx', 0.0) or 0.0):.2f}; "
                            f"plus_di={float(position.get('signal_plus_di', 0.0) or 0.0):.2f}; "
                            f"minus_di={float(position.get('signal_minus_di', 0.0) or 0.0):.2f}"
                        )
                    )
                    self._notify_dashboard_trade_close(script_name, position, current_price)
                    self._close_all_option_for_script(script_name, "FUTURES_EXIT")
                    del self.positions[script_name]
                    self.save_state()
                    # Immediately take reversal trade if crossover exit
                    if crossover_exit:
                        # Simulate reversal entry on this candle close
                        # Set up entry_signal and entry_crossover for reversal
                        reversal_signal = -1 if position['type'] == 'BUY' else 1
                        reversal_crossover = True
                        # Use the same data/candle for entry
                        data['entry_signal'] = reversal_signal
                        data['entry_crossover'] = reversal_crossover
                        data['entry_candle_timestamp'] = last_closed_candle['timestamp']
                        # Recursively call process_script to take reversal
                        self.process_script(script_name, data['instrument_key'])
                    continue
            
            else:
                # Enter new position on crossover
                if not allow_new_entries:
                    continue

                if self._is_before_segment_entry_start(script_name, now_ist):
                    continue

                if self._is_after_segment_cutoff(script_name, now_ist):
                    continue

                latest_timestamp = data.get('entry_candle_timestamp')
                warmup_timestamp = self.entry_warmup_timestamps.get(script_name)
                if latest_timestamp is not None and warmup_timestamp is not None and latest_timestamp <= warmup_timestamp:
                    continue

                entry_candle_timestamp = data.get('entry_candle_timestamp')
                if entry_candle_timestamp is None:
                    continue

                last_processed = self.last_entry_candle_processed.get(script_name)
                if last_processed is not None and entry_candle_timestamp <= last_processed:
                    continue

                entry_signal = data.get('entry_signal', signal)
                entry_crossover = data.get('entry_crossover', crossover)
                entry_ema_short = float(data.get('entry_ema_short', data.get('ema_short', 0.0)))
                entry_ema_long = float(data.get('entry_ema_long', data.get('ema_long', 0.0)))
                entry_ema_long_prev = float(data.get('entry_ema_long_prev', entry_ema_long))
                entry_adx = float(data.get('entry_adx', 0.0) or 0.0)
                entry_plus_di = float(data.get('entry_plus_di', 0.0) or 0.0)
                entry_minus_di = float(data.get('entry_minus_di', 0.0) or 0.0)
                entry_price = current_price
                signal_df = data.get('df')
                level_metrics = self._compute_percent_level_metrics(
                    signal_df,
                    entry_candle_timestamp,
                    entry_price,
                )
                levels_ctx = self._build_percent_levels_context(level_metrics)
                chart_percent = None
                chart_volume = None
                if entry_signal == 1:
                    chart_percent, chart_volume = self._compute_chart_ob_snapshot(
                        signal_df, entry_candle_timestamp, 'BUY'
                    )
                elif entry_signal == -1:
                    chart_percent, chart_volume = self._compute_chart_ob_snapshot(
                        signal_df, entry_candle_timestamp, 'SELL'
                    )

                if entry_crossover:
                    # --- EMA Slope filter: EMA18 must slope in trade direction ---
                    # --- EMA Separation filter: EMA5-EMA18 gap must be meaningful ---
                    min_sep_pct = self._get_min_ema_separation_percent(script_name)
                    ema_sep_pct = abs(entry_ema_short - entry_ema_long) / entry_ema_long * 100 if entry_ema_long > 0 else 0.0

                    if entry_signal == 1:
                        ema_slope_ok = entry_ema_long > entry_ema_long_prev  # EMA18 rising
                        if not ema_slope_ok:
                            trade_prob, trade_prob_bucket = self._estimate_trade_probability(
                                script_name, False, ema_sep_pct, min_sep_pct, 0.0, level_metrics
                            )
                            skip_extra = (
                                f"adx={entry_adx:.2f}; plus_di={entry_plus_di:.2f}; minus_di={entry_minus_di:.2f}; "
                                f"ema18={entry_ema_long:.4f}; prev={entry_ema_long_prev:.4f}; "
                                f"ema_sep_pct={ema_sep_pct:.4f}; trade_prob={trade_prob:.1f}; "
                                f"trade_prob_bucket={trade_prob_bucket}; chart_pct={chart_percent}; "
                                f"chart_vol={chart_volume}; {levels_ctx}"
                            )
                            self._log_skip_event(
                                script_name, "BUY", entry_price, "EMA18_NOT_RISING", skip_extra
                            )
                            self._bot_logger.info(
                                f"SKIP: {script_name} BUY ignored — EMA18 not rising "
                                f"(ema18={entry_ema_long:.4f}, prev={entry_ema_long_prev:.4f})"
                            )
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            continue
                        if ema_sep_pct < min_sep_pct:
                            trade_prob, trade_prob_bucket = self._estimate_trade_probability(
                                script_name, True, ema_sep_pct, min_sep_pct, 0.0, level_metrics
                            )
                            skip_extra = (
                                f"adx={entry_adx:.2f}; plus_di={entry_plus_di:.2f}; minus_di={entry_minus_di:.2f}; "
                                f"ema_sep_pct={ema_sep_pct:.4f}; min_sep_pct={min_sep_pct:.4f}; "
                                f"trade_prob={trade_prob:.1f}; trade_prob_bucket={trade_prob_bucket}; "
                                f"chart_pct={chart_percent}; chart_vol={chart_volume}; {levels_ctx}"
                            )
                            self._log_skip_event(
                                script_name, "BUY", entry_price, "EMA_SEPARATION_TOO_SMALL", skip_extra
                            )
                            self._bot_logger.info(
                                f"SKIP: {script_name} BUY ignored — EMA separation too small "
                                f"(sep={ema_sep_pct:.4f}% < min={min_sep_pct:.4f}%)"
                            )
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            continue

                    elif entry_signal == -1:
                        ema_slope_ok = entry_ema_long < entry_ema_long_prev  # EMA18 falling
                        if not ema_slope_ok:
                            trade_prob, trade_prob_bucket = self._estimate_trade_probability(
                                script_name, False, ema_sep_pct, min_sep_pct, 0.0, level_metrics
                            )
                            skip_extra = (
                                f"adx={entry_adx:.2f}; plus_di={entry_plus_di:.2f}; minus_di={entry_minus_di:.2f}; "
                                f"ema18={entry_ema_long:.4f}; prev={entry_ema_long_prev:.4f}; "
                                f"ema_sep_pct={ema_sep_pct:.4f}; trade_prob={trade_prob:.1f}; "
                                f"trade_prob_bucket={trade_prob_bucket}; chart_pct={chart_percent}; "
                                f"chart_vol={chart_volume}; {levels_ctx}"
                            )
                            self._log_skip_event(
                                script_name, "SELL", entry_price, "EMA18_NOT_FALLING", skip_extra
                            )
                            self._bot_logger.info(
                                f"SKIP: {script_name} SELL ignored — EMA18 not falling "
                                f"(ema18={entry_ema_long:.4f}, prev={entry_ema_long_prev:.4f})"
                            )
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            continue
                        if ema_sep_pct < min_sep_pct:
                            trade_prob, trade_prob_bucket = self._estimate_trade_probability(
                                script_name, True, ema_sep_pct, min_sep_pct, 0.0, level_metrics
                            )
                            skip_extra = (
                                f"adx={entry_adx:.2f}; plus_di={entry_plus_di:.2f}; minus_di={entry_minus_di:.2f}; "
                                f"ema_sep_pct={ema_sep_pct:.4f}; min_sep_pct={min_sep_pct:.4f}; "
                                f"trade_prob={trade_prob:.1f}; trade_prob_bucket={trade_prob_bucket}; "
                                f"chart_pct={chart_percent}; chart_vol={chart_volume}; {levels_ctx}"
                            )
                            self._log_skip_event(
                                script_name, "SELL", entry_price, "EMA_SEPARATION_TOO_SMALL", skip_extra
                            )
                            self._bot_logger.info(
                                f"SKIP: {script_name} SELL ignored — EMA separation too small "
                                f"(sep={ema_sep_pct:.4f}% < min={min_sep_pct:.4f}%)"
                            )
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            continue

                    if self.config.get('adx_filter_enabled', False):
                        min_adx = self._get_adx_min_threshold(script_name)
                        if entry_adx < min_adx:
                            trade_prob, trade_prob_bucket = self._estimate_trade_probability(
                                script_name, True, ema_sep_pct, min_sep_pct, 0.0, level_metrics
                            )
                            side_text = "BUY" if entry_signal == 1 else "SELL"
                            skip_extra = (
                                f"adx={entry_adx:.2f}; min_adx={min_adx:.2f}; "
                                f"plus_di={entry_plus_di:.2f}; minus_di={entry_minus_di:.2f}; "
                                f"ema_sep_pct={ema_sep_pct:.4f}; min_sep_pct={min_sep_pct:.4f}; "
                                f"trade_prob={trade_prob:.1f}; trade_prob_bucket={trade_prob_bucket}; "
                                f"chart_pct={chart_percent}; chart_vol={chart_volume}; {levels_ctx}"
                            )
                            self._log_skip_event(
                                script_name, side_text, entry_price, "ADX_TOO_WEAK", skip_extra
                            )
                            self._bot_logger.info(
                                f"SKIP: {script_name} {side_text} ignored - ADX too weak "
                                f"(adx={entry_adx:.2f} < min={min_adx:.2f})"
                            )
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            continue

                    if entry_signal == 1:
                        initial_sl = self._get_entry_swing_sl(signal_df, entry_candle_timestamp, 'BUY')
                        if initial_sl is None or initial_sl >= entry_price:
                            trade_prob, trade_prob_bucket = self._estimate_trade_probability(
                                script_name, True, ema_sep_pct, min_sep_pct, 0.0, level_metrics
                            )
                            skip_extra = (
                                f"adx={entry_adx:.2f}; plus_di={entry_plus_di:.2f}; minus_di={entry_minus_di:.2f}; "
                                f"sl={initial_sl}; entry={entry_price:.2f}; trade_prob={trade_prob:.1f}; "
                                f"trade_prob_bucket={trade_prob_bucket}; chart_pct={chart_percent}; "
                                f"chart_vol={chart_volume}; {levels_ctx}"
                            )
                            self._log_skip_event(
                                script_name, "BUY", entry_price, "INVALID_SWING_SL", skip_extra
                            )
                            self._bot_logger.info(
                                f"SKIP: {script_name} BUY ignored due to invalid swing SL "
                                f"(sl={initial_sl}, entry={entry_price:.2f})"
                            )
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            continue
                        ob_percent = float(chart_percent) if chart_percent is not None else 100.0
                        trade_prob, trade_prob_bucket = self._estimate_trade_probability(
                            script_name, True, ema_sep_pct, min_sep_pct, ob_percent, level_metrics
                        )
                        qty = self._get_order_quantity(script_name)
                        _, nse_target = self._nse_rupee_sl_target_prices(
                            script_name=script_name,
                            position_type='BUY',
                            entry_price=entry_price,
                            quantity=qty,
                        )
                        target_price = nse_target if nse_target is not None else (
                            entry_price * (1 + self.config['target_percent'] / 100)
                        )
                        self._bot_logger.info(f"BUY signal for {script_name} at {entry_price:.2f}")
                        if is_paper_script(script_name):
                            signal_timestamp = entry_candle_timestamp
                            signal_timestamp_str = (
                                signal_timestamp.isoformat()
                                if signal_timestamp is not None
                                else datetime.now().isoformat()
                            )
                            self.paper_positions[script_name] = {
                                "type": "BUY",
                                "entry_price": entry_price,
                                "entry_time": datetime.now().isoformat(),
                                "quantity": self._get_order_quantity(script_name),
                                "signal_time": signal_timestamp_str,
                                "signal_ema_short": entry_ema_short,
                                "signal_ema_long": entry_ema_long,
                                "signal_adx": entry_adx,
                                "signal_plus_di": entry_plus_di,
                                "signal_minus_di": entry_minus_di,
                                "chart_percent": chart_percent,
                                "chart_volume": chart_volume,
                                "win_percent": trade_prob,
                                "win_percent_source": "model_v2",
                                "ob_percent": ob_percent,
                                "initial_sl": initial_sl,
                                "stop_loss": initial_sl,
                                "target_price": target_price,
                                "trail_steps_locked": 0,
                                "breakeven_done": False,
                                "last_polled_price": float(entry_price),
                            }
                            self.paper_positions[script_name]["trade_id"] = self._build_trade_id(
                                script_name, self.paper_positions[script_name]["entry_time"]
                            )
                            self._bot_logger.info(
                                f" {script_name}: Initial SL set @ Rs{initial_sl:.2f} [PAPER]"
                            )
                            entry_extra = (
                                f"sl={initial_sl:.2f}; target={target_price:.2f}; "
                                f"ob_pct={ob_percent:.2f}; "
                                f"chart_pct={chart_percent}; "
                                f"chart_vol={chart_volume}; "
                                f"ema_sep_pct={ema_sep_pct:.4f}; "
                                f"adx={entry_adx:.2f}; plus_di={entry_plus_di:.2f}; minus_di={entry_minus_di:.2f}; "
                                f"trade_prob={trade_prob:.1f}; trade_prob_bucket={trade_prob_bucket}; "
                                f"{levels_ctx}; "
                                f"ema18_slope=UP({entry_ema_long - entry_ema_long_prev:+.4f}); "
                                f"signal_time={signal_timestamp_str}; "
                                f"order_id=paper; "
                                f"ema{self.config['ema_short']}={entry_ema_short:.2f}; "
                                f"ema{self.config['ema_long']}={entry_ema_long:.2f}; "
                                f"ema{self.config['ema_long']}_prev={entry_ema_long_prev:.2f}"
                            )
                            self._log_paper_order_event(
                                script_name,
                                "PAPER_ENTRY",
                                "BUY",
                                entry_price,
                                "EMA_CROSSOVER",
                                extra=entry_extra,
                            )
                            if telegram_notifications_enabled_for_user(self.username):
                                if not send_paper_trade_notification(
                                    {
                                        "account": self.username,
                                        "symbol": script_name,
                                        "action": "BUY",
                                        "quantity": self._get_order_quantity(script_name),
                                        "price": entry_price,
                                        "reason": "EMA_CROSSOVER",
                                        "stop_loss": initial_sl,
                                        "target_price": target_price,
                                        "win_percent": trade_prob,
                                        "chart_percent": chart_percent,
                                        "chart_volume": chart_volume,
                                        "entry_adx": entry_adx,
                                        "entry_plus_di": entry_plus_di,
                                        "entry_minus_di": entry_minus_di,
                                        "timestamp": self._now_ist(),
                                    },
                                    is_entry=True,
                                ):
                                    self._bot_logger.error(
                                        f"Failed Telegram PAPER ENTRY: {script_name} BUY @ Rs{entry_price:.2f}"
                                    )
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            self.save_state()
                            continue
                        success, order_result = self._place_order_with_result(
                            script_name,
                            "BUY",
                            entry_price,
                            "EMA_CROSSOVER",
                            stop_loss=initial_sl,
                            target_price=target_price,
                            win_percent=trade_prob,
                            chart_percent=chart_percent,
                            chart_volume=chart_volume,
                            entry_adx=entry_adx,
                            entry_plus_di=entry_plus_di,
                            entry_minus_di=entry_minus_di,
                        )
                        if not success:
                            if self._is_mcx_manual_track_candidate(script_name, order_result):
                                signal_timestamp = entry_candle_timestamp
                                signal_timestamp_str = signal_timestamp.isoformat() if signal_timestamp is not None else datetime.now().isoformat()
                                self.positions[script_name] = {
                                    'type': 'BUY',
                                    'entry_price': entry_price,
                                    'entry_time': datetime.now().isoformat(),
                                    'quantity': self._get_order_quantity(script_name),
                                    'signal_time': signal_timestamp_str,
                                    'signal_ema_short': entry_ema_short,
                                    'signal_ema_long': entry_ema_long,
                                    'signal_adx': entry_adx,
                                    'signal_plus_di': entry_plus_di,
                                    'signal_minus_di': entry_minus_di,
                                    'chart_percent': chart_percent,
                                    'chart_volume': chart_volume,
                                    'win_percent': trade_prob,
                                    'win_percent_source': 'model_v2',
                                    'ob_percent': ob_percent,
                                    'initial_sl': initial_sl,
                                    'stop_loss': initial_sl,
                                    'target_price': target_price,
                                    'trail_steps_locked': 0,
                                    'breakeven_done': False,
                                    'last_polled_price': float(entry_price),
                                    'manual_execution': True,
                                }
                                self.positions[script_name]['trade_id'] = self._build_trade_id(
                                    script_name, self.positions[script_name]['entry_time']
                                )
                                self._bot_logger.warning(
                                    f"MANUAL TRACK: {script_name} BUY virtual position started after API failure; "
                                    f"waiting to notify manual close on exit conditions."
                                )
                                self._order_logger.info(
                                    f"{script_name} | ACTION=MANUAL_TRACK_START | SIDE=BUY | PRICE={entry_price:.2f} "
                                    f"| REASON=MCX_API_DISABLED | sl={initial_sl:.2f}; target={target_price:.2f}"
                                )
                                self._notify_dashboard_trade_open(
                                    script_name, self.positions[script_name], entry_price
                                )
                                self.save_state()
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            continue
                        signal_timestamp = entry_candle_timestamp
                        signal_timestamp_str = signal_timestamp.isoformat() if signal_timestamp is not None else datetime.now().isoformat()
                        order_id = (order_result.get('data') or {}).get('order_id', 'NA')
                        self.positions[script_name] = {
                            'type': 'BUY',
                            'entry_price': entry_price,
                            'entry_time': datetime.now().isoformat(),
                            'quantity': self._get_order_quantity(script_name),
                            'signal_time': signal_timestamp_str,
                            'signal_ema_short': entry_ema_short,
                            'signal_ema_long': entry_ema_long,
                            'signal_adx': entry_adx,
                            'signal_plus_di': entry_plus_di,
                            'signal_minus_di': entry_minus_di,
                            'chart_percent': chart_percent,
                            'chart_volume': chart_volume,
                            'win_percent': trade_prob,
                            'win_percent_source': 'model_v2',
                            'ob_percent': ob_percent,
                            'initial_sl': initial_sl,
                            'stop_loss': initial_sl,
                            'target_price': target_price,
                            'trail_steps_locked': 0,
                            'breakeven_done': False,
                            'last_polled_price': float(entry_price)
                        }
                        self.positions[script_name]['trade_id'] = self._build_trade_id(
                            script_name, self.positions[script_name]['entry_time']
                        )
                        self._bot_logger.info(f" {script_name}: Initial SL set @ Rs{initial_sl:.2f}")
                        self._log_order_event(
                            script_name,
                            action="ENTRY",
                            side="BUY",
                            price=entry_price,
                            reason="EMA_CROSSOVER",
                            extra=(
                                f"sl={initial_sl:.2f}; target={target_price:.2f}; "
                                f"ob_pct={ob_percent:.2f}; "
                                f"chart_pct={chart_percent}; "
                                f"chart_vol={chart_volume}; "
                                f"ema_sep_pct={ema_sep_pct:.4f}; "
                                f"adx={entry_adx:.2f}; plus_di={entry_plus_di:.2f}; minus_di={entry_minus_di:.2f}; "
                                f"trade_prob={trade_prob:.1f}; trade_prob_bucket={trade_prob_bucket}; "
                                f"{levels_ctx}; "
                                f"ema18_slope=UP({entry_ema_long - entry_ema_long_prev:+.4f}); "
                                f"signal_time={signal_timestamp_str}; "
                                f"order_id={order_id}; "
                                f"ema{self.config['ema_short']}={entry_ema_short:.2f}; "
                                f"ema{self.config['ema_long']}={entry_ema_long:.2f}; "
                                f"ema{self.config['ema_long']}_prev={entry_ema_long_prev:.2f}"
                            )
                        )
                        self._start_options_companion(
                            script_name=script_name,
                            futures_position=self.positions[script_name],
                            entry_price=entry_price,
                            initial_sl=initial_sl,
                        )
                        self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                        self.save_state()
                        self._notify_dashboard_trade_open(
                            script_name, self.positions[script_name], entry_price
                        )
                    
                    elif entry_signal == -1:
                        initial_sl = self._get_entry_swing_sl(signal_df, entry_candle_timestamp, 'SELL')
                        if initial_sl is None or initial_sl <= entry_price:
                            trade_prob, trade_prob_bucket = self._estimate_trade_probability(
                                script_name, True, ema_sep_pct, min_sep_pct, 0.0, level_metrics
                            )
                            skip_extra = (
                                f"adx={entry_adx:.2f}; plus_di={entry_plus_di:.2f}; minus_di={entry_minus_di:.2f}; "
                                f"sl={initial_sl}; entry={entry_price:.2f}; trade_prob={trade_prob:.1f}; "
                                f"trade_prob_bucket={trade_prob_bucket}; chart_pct={chart_percent}; "
                                f"chart_vol={chart_volume}; {levels_ctx}"
                            )
                            self._log_skip_event(
                                script_name, "SELL", entry_price, "INVALID_SWING_SL", skip_extra
                            )
                            self._bot_logger.info(
                                f"SKIP: {script_name} SELL ignored due to invalid swing SL "
                                f"(sl={initial_sl}, entry={entry_price:.2f})"
                            )
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            continue
                        ob_percent = float(chart_percent) if chart_percent is not None else 100.0
                        trade_prob, trade_prob_bucket = self._estimate_trade_probability(
                            script_name, True, ema_sep_pct, min_sep_pct, ob_percent, level_metrics
                        )
                        qty = self._get_order_quantity(script_name)
                        _, nse_target = self._nse_rupee_sl_target_prices(
                            script_name=script_name,
                            position_type='SELL',
                            entry_price=entry_price,
                            quantity=qty,
                        )
                        target_price = nse_target if nse_target is not None else (
                            entry_price * (1 - self.config['target_percent'] / 100)
                        )
                        self._bot_logger.info(f"SELL signal for {script_name} at {entry_price:.2f}")
                        if is_paper_script(script_name):
                            signal_timestamp = entry_candle_timestamp
                            signal_timestamp_str = (
                                signal_timestamp.isoformat()
                                if signal_timestamp is not None
                                else datetime.now().isoformat()
                            )
                            self.paper_positions[script_name] = {
                                "type": "SELL",
                                "entry_price": entry_price,
                                "entry_time": datetime.now().isoformat(),
                                "quantity": self._get_order_quantity(script_name),
                                "signal_time": signal_timestamp_str,
                                "signal_ema_short": entry_ema_short,
                                "signal_ema_long": entry_ema_long,
                                "signal_adx": entry_adx,
                                "signal_plus_di": entry_plus_di,
                                "signal_minus_di": entry_minus_di,
                                "chart_percent": chart_percent,
                                "chart_volume": chart_volume,
                                "win_percent": trade_prob,
                                "win_percent_source": "model_v2",
                                "ob_percent": ob_percent,
                                "initial_sl": initial_sl,
                                "stop_loss": initial_sl,
                                "target_price": target_price,
                                "trail_steps_locked": 0,
                                "breakeven_done": False,
                                "last_polled_price": float(entry_price),
                            }
                            self.paper_positions[script_name]["trade_id"] = self._build_trade_id(
                                script_name, self.paper_positions[script_name]["entry_time"]
                            )
                            self._bot_logger.info(
                                f" {script_name}: Initial SL set @ Rs{initial_sl:.2f} [PAPER]"
                            )
                            entry_extra = (
                                f"sl={initial_sl:.2f}; target={target_price:.2f}; "
                                f"ob_pct={ob_percent:.2f}; "
                                f"chart_pct={chart_percent}; "
                                f"chart_vol={chart_volume}; "
                                f"ema_sep_pct={ema_sep_pct:.4f}; "
                                f"adx={entry_adx:.2f}; plus_di={entry_plus_di:.2f}; minus_di={entry_minus_di:.2f}; "
                                f"trade_prob={trade_prob:.1f}; trade_prob_bucket={trade_prob_bucket}; "
                                f"{levels_ctx}; "
                                f"ema18_slope=DOWN({entry_ema_long - entry_ema_long_prev:+.4f}); "
                                f"signal_time={signal_timestamp_str}; "
                                f"order_id=paper; "
                                f"ema{self.config['ema_short']}={entry_ema_short:.2f}; "
                                f"ema{self.config['ema_long']}={entry_ema_long:.2f}; "
                                f"ema{self.config['ema_long']}_prev={entry_ema_long_prev:.2f}"
                            )
                            self._log_paper_order_event(
                                script_name,
                                "PAPER_ENTRY",
                                "SELL",
                                entry_price,
                                "EMA_CROSSOVER",
                                extra=entry_extra,
                            )
                            if telegram_notifications_enabled_for_user(self.username):
                                if not send_paper_trade_notification(
                                    {
                                        "account": self.username,
                                        "symbol": script_name,
                                        "action": "SELL",
                                        "quantity": self._get_order_quantity(script_name),
                                        "price": entry_price,
                                        "reason": "EMA_CROSSOVER",
                                        "stop_loss": initial_sl,
                                        "target_price": target_price,
                                        "win_percent": trade_prob,
                                        "chart_percent": chart_percent,
                                        "chart_volume": chart_volume,
                                        "entry_adx": entry_adx,
                                        "entry_plus_di": entry_plus_di,
                                        "entry_minus_di": entry_minus_di,
                                        "timestamp": self._now_ist(),
                                    },
                                    is_entry=True,
                                ):
                                    self._bot_logger.error(
                                        f"Failed Telegram PAPER ENTRY: {script_name} SELL @ Rs{entry_price:.2f}"
                                    )
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            self.save_state()
                            continue
                        success, order_result = self._place_order_with_result(
                            script_name,
                            "SELL",
                            entry_price,
                            "EMA_CROSSOVER",
                            stop_loss=initial_sl,
                            target_price=target_price,
                            win_percent=trade_prob,
                            chart_percent=chart_percent,
                            chart_volume=chart_volume,
                            entry_adx=entry_adx,
                            entry_plus_di=entry_plus_di,
                            entry_minus_di=entry_minus_di,
                        )
                        if not success:
                            if self._is_mcx_manual_track_candidate(script_name, order_result):
                                signal_timestamp = entry_candle_timestamp
                                signal_timestamp_str = signal_timestamp.isoformat() if signal_timestamp is not None else datetime.now().isoformat()
                                self.positions[script_name] = {
                                    'type': 'SELL',
                                    'entry_price': entry_price,
                                    'entry_time': datetime.now().isoformat(),
                                    'quantity': self._get_order_quantity(script_name),
                                    'signal_time': signal_timestamp_str,
                                    'signal_ema_short': entry_ema_short,
                                    'signal_ema_long': entry_ema_long,
                                    'signal_adx': entry_adx,
                                    'signal_plus_di': entry_plus_di,
                                    'signal_minus_di': entry_minus_di,
                                    'chart_percent': chart_percent,
                                    'chart_volume': chart_volume,
                                    'win_percent': trade_prob,
                                    'win_percent_source': 'model_v2',
                                    'ob_percent': ob_percent,
                                    'initial_sl': initial_sl,
                                    'stop_loss': initial_sl,
                                    'target_price': target_price,
                                    'trail_steps_locked': 0,
                                    'breakeven_done': False,
                                    'last_polled_price': float(entry_price),
                                    'manual_execution': True,
                                }
                                self.positions[script_name]['trade_id'] = self._build_trade_id(
                                    script_name, self.positions[script_name]['entry_time']
                                )
                                self._bot_logger.warning(
                                    f"MANUAL TRACK: {script_name} SELL virtual position started after API failure; "
                                    f"waiting to notify manual close on exit conditions."
                                )
                                self._order_logger.info(
                                    f"{script_name} | ACTION=MANUAL_TRACK_START | SIDE=SELL | PRICE={entry_price:.2f} "
                                    f"| REASON=MCX_API_DISABLED | sl={initial_sl:.2f}; target={target_price:.2f}"
                                )
                                self._notify_dashboard_trade_open(
                                    script_name, self.positions[script_name], entry_price
                                )
                                self.save_state()
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            continue
                        signal_timestamp = entry_candle_timestamp
                        signal_timestamp_str = signal_timestamp.isoformat() if signal_timestamp is not None else datetime.now().isoformat()
                        order_id = (order_result.get('data') or {}).get('order_id', 'NA')
                        self.positions[script_name] = {
                            'type': 'SELL',
                            'entry_price': entry_price,
                            'entry_time': datetime.now().isoformat(),
                            'quantity': self._get_order_quantity(script_name),
                            'signal_time': signal_timestamp_str,
                            'signal_ema_short': entry_ema_short,
                            'signal_ema_long': entry_ema_long,
                            'signal_adx': entry_adx,
                            'signal_plus_di': entry_plus_di,
                            'signal_minus_di': entry_minus_di,
                            'chart_percent': chart_percent,
                            'chart_volume': chart_volume,
                            'win_percent': trade_prob,
                            'win_percent_source': 'model_v2',
                            'ob_percent': ob_percent,
                            'initial_sl': initial_sl,
                            'stop_loss': initial_sl,
                            'target_price': target_price,
                            'trail_steps_locked': 0,
                            'breakeven_done': False,
                            'last_polled_price': float(entry_price)
                        }
                        self.positions[script_name]['trade_id'] = self._build_trade_id(
                            script_name, self.positions[script_name]['entry_time']
                        )
                        self._bot_logger.info(f" {script_name}: Initial SL set @ Rs{initial_sl:.2f}")
                        self._log_order_event(
                            script_name,
                            action="ENTRY",
                            side="SELL",
                            price=entry_price,
                            reason="EMA_CROSSOVER",
                            extra=(
                                f"sl={initial_sl:.2f}; target={target_price:.2f}; "
                                f"ob_pct={ob_percent:.2f}; "
                                f"chart_pct={chart_percent}; "
                                f"chart_vol={chart_volume}; "
                                f"ema_sep_pct={ema_sep_pct:.4f}; "
                                f"adx={entry_adx:.2f}; plus_di={entry_plus_di:.2f}; minus_di={entry_minus_di:.2f}; "
                                f"trade_prob={trade_prob:.1f}; trade_prob_bucket={trade_prob_bucket}; "
                                f"{levels_ctx}; "
                                f"ema18_slope=DOWN({entry_ema_long - entry_ema_long_prev:+.4f}); "
                                f"signal_time={signal_timestamp_str}; "
                                f"order_id={order_id}; "
                                f"ema{self.config['ema_short']}={entry_ema_short:.2f}; "
                                f"ema{self.config['ema_long']}={entry_ema_long:.2f}; "
                                f"ema{self.config['ema_long']}_prev={entry_ema_long_prev:.2f}"
                            )
                        )
                        self._start_options_companion(
                            script_name=script_name,
                            futures_position=self.positions[script_name],
                            entry_price=entry_price,
                            initial_sl=initial_sl,
                        )
                        self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                        self.save_state()
                        self._notify_dashboard_trade_open(
                            script_name, self.positions[script_name], entry_price
                        )
    
    def _wait_for_upstox(self) -> None:
        """Block until this user's token works or self.running is False."""
        while self.running:
            self.client.refresh_credentials_if_changed()
            profile = self.client.get_user_profile()
            if profile:
                self._bot_logger.info(
                    "CONNECTED: %s as Upstox user %s",
                    self.username,
                    profile.get("user_name", "Unknown"),
                )
                return
            self._bot_logger.warning(
                "Upstox login failed for [%s] (invalid or expired token). File: %s — "
                "save token in dashboard for this user. Retrying in 30s...",
                self.username,
                credentials_file_for_user(self.username).resolve(),
            )
            for _ in range(30):
                if not self.running:
                    return
                time.sleep(1)

    def _apply_manual_controls_from_disk(self) -> None:
        path = user_data_dir(self.username) / "manual_trade_controls.json"
        if not path.is_file():
            return
        try:
            controls = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        ignored_ids = {str(x) for x in (controls.get("ignored_trade_ids") or []) if str(x).strip()}
        overrides = controls.get("entry_price_overrides") or {}
        changed = False
        for script_name in list(self.positions.keys()):
            pos = self.positions.get(script_name)
            if not isinstance(pos, dict) or not bool(pos.get("manual_execution")):
                continue
            trade_id = str(pos.get("trade_id") or "")
            if trade_id and trade_id in ignored_ids:
                self._bot_logger.info(
                    f"MANUAL TRACK DISMISSED from dashboard: {script_name} trade_id={trade_id}"
                )
                self.positions.pop(script_name, None)
                changed = True
                continue
            ov = overrides.get(trade_id)
            if ov is None:
                continue
            try:
                new_entry = float(ov)
            except (TypeError, ValueError):
                continue
            if new_entry <= 0:
                continue
            old_entry = float(pos.get("entry_price") or 0.0)
            if abs(new_entry - old_entry) < 1e-9:
                continue
            pos["entry_price"] = new_entry
            pos["manual_entry_price"] = new_entry
            pos["manual_entry_updated_at"] = datetime.now().isoformat()
            self._bot_logger.info(
                f"MANUAL ENTRY OVERRIDE applied: {script_name} {old_entry:.2f} -> {new_entry:.2f}"
            )
            changed = True
        if changed:
            self.save_state()

    def _scripts_for_cycle(self) -> list[tuple[str, str]]:
        """Symbols to fetch this loop: user scope ∪ open positions (order preserved from TRADING_CONFIG)."""
        prefs = read_trading_preferences(self.username)
        raw = prefs.get("enabled_scripts")
        all_items = list(self.config["scripts"].items())
        order_names = [n for n, _ in all_items]
        if raw is None:
            chosen = set(order_names)
        else:
            keys = self.config["scripts"].keys()
            chosen = {
                str(x).strip().upper()
                for x in raw
                if str(x).strip().upper() in keys
            }
            if not chosen:
                chosen = set(order_names)
        for sym in self.positions.keys():
            chosen.add(sym)
        for sym in self.paper_positions.keys():
            chosen.add(sym)
        out: list[tuple[str, str]] = []
        for name, key in all_items:
            if name in chosen:
                out.append((name, key))
        return out

    def _run_one_cycle(self) -> str:
        """
        Single iteration. Returns:
        - "ok" — continue
        - "stop_bot" — portfolio stop for this user only
        - "shutdown_all" — daily shutdown (runner stops every account)
        """
        self.client.refresh_credentials_if_changed()
        self._maybe_refresh_index_futures_tokens_expiry_day()
        self._apply_manual_controls_from_disk()
        cycle_items = list(self._scripts_for_cycle())
        if self._market_data_is_kite():
            self._ensure_kite_tick_feed([n for n, _ in cycle_items])
        if not self._cycle_scope_logged_once:
            self._cycle_scope_logged_once = True
            self._bot_logger.info(
                "CYCLE_SCOPE first loop: count=%d symbols=%s",
                len(cycle_items),
                ",".join(n for n, _ in cycle_items),
            )
        batch_sleep = float(os.environ.get("UPSTOX_MARKET_FETCH_BATCH_SLEEP_SEC", "0.12"))
        batch_size = max(1, int(os.environ.get("UPSTOX_MARKET_FETCH_BATCH_SIZE", "10")))
        script_data = []
        for i, (script_name, instrument_key) in enumerate(cycle_items):
            if i > 0 and i % batch_size == 0 and batch_sleep > 0:
                time.sleep(batch_sleep)
            data = self.process_script(script_name, instrument_key)
            script_data.append(data)
            if data:
                self._script_data_cache[str(script_name or "").strip().upper()] = data

        self.print_status_table(script_data)

        allow_new_entries = self.entry_warmup_done

        if not self.entry_warmup_done:
            for data in script_data:
                if data:
                    self.entry_warmup_timestamps[data["script_name"]] = data.get(
                        "entry_candle_timestamp"
                    )
            self.entry_warmup_done = True
            self._bot_logger.info(
                "ENTRY WARMUP: Startup snapshot captured. New entries will trigger only on fresh crossover candles."
            )

        latest_prices = {
            data["script_name"]: data["current_price"]
            for data in script_data
            if data and data.get("current_price") is not None
        }

        now_ist = self._now_ist()
        self._run_eod_squareoff(now_ist, latest_prices=latest_prices)

        with self._strategy_lock:
            self.execute_trading_logic(
                script_data, allow_new_entries=allow_new_entries, now_ist=now_ist
            )
        self._manage_option_positions(latest_prices)

        # Paper live P&L on the dashboard reads trading_state.json; last_polled_price updates in RAM
        # each loop but was not persisted unless save_state ran elsewhere — sync LTP and flush.
        if self.paper_positions:
            for _sym, _pos in self.paper_positions.items():
                _lp = latest_prices.get(_sym)
                if _lp is not None:
                    _pos["last_polled_price"] = float(_lp)
            self.save_state()

        for script_name, position in self.positions.items():
            current_price = latest_prices.get(script_name)
            if current_price is None:
                continue
            self._queue_dashboard_trade_update(script_name, position, current_price)
        self._flush_dashboard_trade_updates()

        if self.total_pnl < -self.config["portfolio_stop_loss"]:
            self._bot_logger.error(
                f" Portfolio stop loss hit! Total loss: Rs{self.total_pnl:.2f}"
            )
            self._bot_logger.error(" Exiting all positions and stopping this account.")
            for _s in list(self.option_positions.keys()):
                self._close_all_option_for_script(_s, "PORTFOLIO_STOP", force_remove=True)
            self.positions.clear()
            self.save_state()
            self.running = False
            return "stop_bot"

        if self._is_after_daily_shutdown(now_ist):
            shutdown_time_text = self.config.get("daily_shutdown_time", "23:21")
            self._bot_logger.info(
                f"AUTO SHUTDOWN: Reached {shutdown_time_text} IST. Archiving for [{self.username}]."
            )
            self.running = False
            self.archive_requested = bool(self.config.get("auto_archive_on_shutdown", True))
            return "shutdown_all"

        self._bot_logger.info(
            f"Next update in {self.config['loop_interval']} seconds...\n"
        )
        return "ok"

    def run(self):
        """Main trading loop for this user only."""
        self._bot_logger.info("=" * 80)
        self._bot_logger.info("STARTUP: Trading Bot — account %s", self.username)
        self._bot_logger.info("=" * 80)

        self.load_state()
        self._wait_for_upstox()
        if not self.running:
            return
        if self._market_data_is_kite():
            if not self._get_kite_credentials():
                self._bot_logger.error(
                    "market_data_provider=kite requires zerodha_credentials.json with api_key and access_token. Exiting."
                )
                return
            self._bot_logger.info(
                "MARKET DATA: Zerodha Kite (REST candles + WebSocket LTP); ORDERS: Upstox (unchanged)"
            )
            if self._kite_stream_drive_exits():
                self._bot_logger.info(
                    "KITE_STREAM_DRIVE_EXITS: SL/target (and trailing) run on LTP ticks; "
                    "strategy loop interval=%ss is for candles/entries/OB exits (set KITE_STRATEGY_LOOP_SEC).",
                    self.config.get("loop_interval", 10),
                )
            if self._kite_signal_boundary_wake_enabled():
                self._bot_logger.info(
                    "KITE_SIGNAL_BOUNDARY_WAKE: wake at each %dm bar (signal_interval) + %ss offset so 5m entries "
                    "are not delayed by loop_interval; sleep=min(loop_interval, boundary). Disable: KITE_SIGNAL_BOUNDARY_WAKE=0.",
                    self._signal_bucket_minutes(),
                    float(os.environ.get("KITE_BOUNDARY_EVAL_OFFSET_SEC", "2") or 2),
                )

        try:
            boundary_wake = (
                self._market_data_is_kite() and self._kite_signal_boundary_wake_enabled()
            )
            while self.running:
                try:
                    code = self._run_one_cycle()
                    if code == "shutdown_all":
                        break
                    if code == "stop_bot":
                        break
                    li = float(self.config["loop_interval"])
                    if boundary_wake:
                        bd = self._seconds_until_next_signal_bar_fire_ist()
                        sleep_s = min(li, bd)
                    else:
                        sleep_s = li
                    sleep_s = max(0.05, float(sleep_s))
                    time.sleep(sleep_s)
                except KeyboardInterrupt:
                    self._bot_logger.info(
                        "\n Keyboard interrupt detected. Shutting down gracefully..."
                    )
                    self.running = False
                    break
                except Exception as e:
                    self._bot_logger.error(f" Error in trading loop: {e}")
                    li = float(self.config["loop_interval"])
                    if boundary_wake:
                        sleep_s = max(0.05, min(li, self._seconds_until_next_signal_bar_fire_ist()))
                    else:
                        sleep_s = li
                    time.sleep(sleep_s)

        finally:
            if self._kite_tick_stream is not None:
                try:
                    self._kite_tick_stream.stop()
                except Exception:
                    pass
                self._kite_tick_stream = None
            self.save_state()
            self._bot_logger.info("=" * 80)
            self._bot_logger.info(" Trading Bot Stopped [%s]", self.username)
            self._bot_logger.info("=" * 80)
            if self.archive_requested:
                self._run_daily_archive()


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we may not have permission.
        return True
    except OSError:
        return False


def _acquire_single_instance_lock() -> bool:
    current_pid = os.getpid()
    
    def _write_lock_file() -> None:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            json.dump(
                {"pid": current_pid, "started_at": datetime.now().isoformat()},
                lock_file
            )

    for _ in range(2):
        try:
            _write_lock_file()

            def _release_lock():
                try:
                    if LOCK_FILE.exists():
                        payload = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
                        if int(payload.get("pid", -1)) == current_pid:
                            LOCK_FILE.unlink(missing_ok=True)
                except Exception:
                    pass

            atexit.register(_release_lock)
            return True
        except FileExistsError:
            try:
                payload = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
                existing_pid = int(payload.get("pid", -1))
            except Exception:
                existing_pid = -1

            # Docker commonly runs the app as PID 1. If a previous run left a
            # lock with the same PID, treat it as stale-self and recover.
            if existing_pid == current_pid:
                LOCK_FILE.unlink(missing_ok=True)
                try:
                    _write_lock_file()
                except FileExistsError:
                    continue

                def _release_lock():
                    try:
                        if LOCK_FILE.exists():
                            payload = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
                            if int(payload.get("pid", -1)) == current_pid:
                                LOCK_FILE.unlink(missing_ok=True)
                    except Exception:
                        pass

                atexit.register(_release_lock)
                return True

            # Remove stale or malformed lock and retry once.
            if existing_pid <= 0 or not _pid_is_running(existing_pid):
                LOCK_FILE.unlink(missing_ok=True)
                continue

            print(
                f"Another trading bot instance is already running (PID: {existing_pid}). "
                f"Exiting this launch."
            )
            return False
    return False


def main():
    """Main entry point — single tenant (AK07)."""
    if not _acquire_single_instance_lock():
        return

    print(f"{Fore.CYAN}")
    print("=" * 80)
    print("   MULTI-SCRIPT TRADING BOT v2.0 (AK07)")
    print("   EMA Crossover Strategy")
    print("   " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 80)
    print(f"{Style.RESET_ALL}")

    print(f"{Fore.CYAN}RUNNING SCRIPT:{Style.RESET_ALL} {Path(__file__).resolve()}\n")

    try:
        public_ip = requests.get("https://api.ipify.org", timeout=5).text
        print(f"{Fore.YELLOW} Public IP: {public_ip}{Style.RESET_ALL}\n")
    except Exception:
        pass

    usernames = ["AK07"]
    bots: list[TradingBot] = []
    try:
        poll_s = int(os.environ.get("BOT_CREDENTIALS_POLL_SECONDS", "60"))
    except ValueError:
        poll_s = 60
    poll_s = max(15, poll_s)

    while True:
        bots.clear()
        for un in usernames:
            creds = load_upstox_credentials_for_user(un)
            tok = (creds.get("access_token") or "").strip()
            if not tok:
                continue
            base = creds.get("base_url") or API_CONFIG["base_url"]
            client = UpstoxClient(tok, base, username=un, log=None)
            bot = TradingBot(runtime_trading_config(), client, username=un)
            bots.append(bot)
            cf = credentials_file_for_user(un)
            print(
                f"{Fore.CYAN}[{un}]{Style.RESET_ALL} token preview={mask_tail(tok)} file={cf}"
            )

        if bots:
            break

        print(
            f"{Fore.RED}No Upstox access token. Sign in to the dashboard as AK07 and save Upstox "
            f"credentials (written to {credentials_file_for_user('AK07')}). "
            f"Retrying in {poll_s}s…{Style.RESET_ALL}"
        )
        time.sleep(poll_s)

    if any(telegram_notifications_enabled_for_user(b.username) for b in bots):
        if not send_telegram_test_message():
            print("Telegram test message failed – check bot token / chat ID.")
        else:
            print("Telegram test message sent successfully.")
    else:
        print(f"{Fore.YELLOW}Telegram test skipped.{Style.RESET_ALL}")

    bots[0].run()


if __name__ == "__main__":
    main()
