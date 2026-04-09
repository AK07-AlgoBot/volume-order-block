"""
Multi-Script Trading Bot with EMA Crossover Strategy
Version: 2.0
Created: March 4, 2026
"""

import time
import math
import logging
import json
import sys
import os
import html
import atexit
import gzip
from datetime import datetime, timedelta
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

    if reason in exit_reasons:
        title = "🔴 *Trade Closed*"
    elif reason == "ORDER_FAILED":
        title = "🟠 *Manual Action Required*"
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
    },
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
    },
    "interval": "1minute",  # API fetch interval (Upstox accepts 1minute reliably)
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
    "loop_interval": 10,  # seconds between each check
    "contract_roll_retry_seconds": 300,  # seconds between roll attempts per script
    "contract_roll_mcx_cache_seconds": 21600,  # 6h MCX instrument cache window
    "quantity": 1,  # Number of lots per order
    # Optional per-script exchange quantity override for order placement.
    # Example: CRUDE quantity=1 (instead of lots*lot_size) to match broker setup.
    "order_quantity_override_by_script": {
        "CRUDE": 1
    },
    "segment_scripts": {
        "NSE": [
            "NIFTY", "BANKNIFTY", "SENSEX",
            "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "TCS",
            "INFY", "AXISBANK", "KOTAKBANK", "LT", "ITC",
            "HINDUNILVR", "BAJFINANCE", "BHARTIARTL", "MARUTI", "SUNPHARMA",
            "TITAN", "ULTRACEMCO", "NESTLEIND", "POWERGRID", "HCLTECH",
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

REPO_ROOT = _REPO_ROOT
LOCK_FILE = REPO_ROOT / "src" / "bot" / "trading_bot.lock"

# Console-only bootstrap; per-account file loggers are attached in TradingBot.__init__
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

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
        self._mcx_instruments_cache = []
        self._mcx_instruments_cache_at = 0.0
        self._nse_instruments_cache = []
        self._nse_instruments_cache_at = 0.0
        self._bse_instruments_cache = []
        self._bse_instruments_cache_at = 0.0
        self._cycle_scope_logged_once = False
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
            selected = candidates[0]
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

    def _get_fo_contract_candidates(self, script_name: str) -> list[str]:
        """
        Candidate keys for NSE_FO/BSE_FO rollovers.
        Filters by FUT + correct segment + trading_symbol root + active expiry + lot_size.
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

            # Match root as the first token (e.g. "LT FUT ..."), not substring ("LT" must not match "BOSCHLTD").
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

        # Sort by expiry then key; remove duplicates by key while preserving order.
        candidates.sort(key=lambda item: (item[0], item[1]))
        out: list[str] = []
        seen: set[str] = set()
        for _, key in candidates:
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

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
    
    def fetch_market_data(self, script_name, instrument_key):
        """Fetch and combine historical + intraday market data"""
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
            
            return {
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
    
    def execute_trading_logic(self, script_data, allow_new_entries=True, now_ist=None):
        """Execute trading logic based on signals"""
        if now_ist is None:
            now_ist = self._now_ist()

        for data in script_data:
            if not data:
                continue
            
            script_name = data['script_name']
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
                if confirmed_time_text != 'NA' and last_eval_ts != confirmed_time_text:
                    self._bot_logger.info(
                        f"VERIFY: {script_name} open={position['type']} | entry={position['entry_price']:.2f} | "
                        f"closed_signal={confirmed_signal} | closed_crossover={bool(confirmed_crossover)} | "
                        f"closed_time={confirmed_time_text}"
                    )
                    self.last_position_eval_logged[script_name] = confirmed_time_text

                # Update stepped trailing SL as per strategy
                sl_updated = self._update_position_sl(script_name, position, current_price)
                if sl_updated:
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
                    del self.positions[script_name]
                    self.save_state()
                    continue

                # Carry forward the current observation as baseline for next 10s gap check.
                position['last_polled_price'] = float(current_price)

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
        cycle_items = list(self._scripts_for_cycle())
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

        self.execute_trading_logic(script_data, allow_new_entries=allow_new_entries, now_ist=now_ist)

        # Paper live P&L on the dashboard reads trading_state.json; last_polled_price updates in RAM
        # each loop but was not persisted unless save_state ran elsewhere — sync LTP and flush.
        if self.paper_positions:
            for _sym, _pos in self.paper_positions.items():
                _lp = latest_prices.get(_sym)
                if _lp is not None:
                    _pos["last_polled_price"] = float(_lp)
            self.save_state()

        for script_name, position in self.positions.items():
            if bool(position.get("manual_execution")):
                continue
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

        try:
            while self.running:
                try:
                    code = self._run_one_cycle()
                    if code == "shutdown_all":
                        break
                    if code == "stop_bot":
                        break
                    time.sleep(self.config["loop_interval"])
                except KeyboardInterrupt:
                    self._bot_logger.info(
                        "\n Keyboard interrupt detected. Shutting down gracefully..."
                    )
                    self.running = False
                    break
                except Exception as e:
                    self._bot_logger.error(f" Error in trading loop: {e}")
                    time.sleep(self.config["loop_interval"])

        finally:
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
            bot = TradingBot(TRADING_CONFIG, client, username=un)
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
