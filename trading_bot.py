"""
Multi-Script Trading Bot with EMA Crossover Strategy
Version: 2.0
Created: March 4, 2026
"""

import time
import logging
import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import pandas as pd
import numpy as np
import requests
from colorama import Fore, Style, init

# Initialize colorama
init(autoreset=True)

# ============================================================================
# CONFIGURATION
# ============================================================================

# Upstox API Configuration
API_CONFIG = {
    "access_token": "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI2RUJBVTQiLCJqdGkiOiI2OWFlM2FlMDQyODZjYzJiOGE3MDcyOTEiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlhdCI6MTc3MzAyNjAxNiwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxNzczMDkzNjAwfQ.TAWFRh00tOvI_d_oOSuueX0EOapntZFsBzb9P9n6oDQ",
    "api_key": "d9df59d8-e3c8-491e-9a7a-0bd19805ba8d",
    "api_secret": "wu5npsei6y",
    "base_url": "https://api.upstox.com/v2"
}

# Trading Configuration
TRADING_CONFIG = {
    "scripts": {
        "NIFTY": "NSE_FO|51714",           # NIFTY Futures for data fetching
        "BANKNIFTY": "NSE_FO|51701",       # BANKNIFTY Futures for data fetching
        "SENSEX": "BSE_FO|825565",         # SENSEX Futures for data fetching
        "CRUDE": "MCX_FO|472789",
        "GOLDMINI": "MCX_FO|487665",
        "SILVERMINI": "MCX_FO|457533"
    },
    # Separate tokens for order placement (FUTURES/COMMODITIES)
    "order_tokens": {
        "NIFTY": "NSE_FO|51714",
        "BANKNIFTY": "NSE_FO|51701",
        "SENSEX": "BSE_FO|825565",
        "CRUDE": "MCX_FO|472789",
        "GOLDMINI": "MCX_FO|487665",
        "SILVERMINI": "MCX_FO|457533"
    },
    "lot_sizes": {
        "NIFTY": 65,
        "BANKNIFTY": 30,
        "SENSEX": 20,
        "CRUDE": 100,
        "GOLDMINI": 1,
        "SILVERMINI": 5
    },
    "interval": "1minute",  # API fetch interval (Upstox accepts 1minute reliably)
    "signal_interval": "5minute",  # Strategy timeframe (EMA runs on 5-minute candles)
    "ema_short": 5,
    "ema_long": 18,
    "portfolio_stop_loss": 10000,  # ₹10,000
    "trailing_stop_loss_percent": 1.0,  # 1%
    "trail_step_percent": 0.5,  # After 1:1, trail SL by 0.5% for every 0.5% favorable move
    "target_percent": 2.0,  # Book profit at +2% move (or -2% for SELL)
    "min_ob_percent_by_script": {
        "NIFTY": 0.44,
        "BANKNIFTY": 0.26,
        "SENSEX": 0.11,
        "CRUDE": 0.60,
        "GOLDMINI": 0.25,
        "SILVERMINI": 0.68
    },
    "order_block_lookback_candles": 12,  # Search depth for latest opposite candle (5m) as order block
    "loop_interval": 10,  # seconds between each check
    "quantity": 1,  # Number of lots per order
    "segment_scripts": {
        "NSE": ["NIFTY", "BANKNIFTY", "SENSEX"],
        "MCX": ["CRUDE", "GOLDMINI", "SILVERMINI"]
    },
    "entry_start_times": {
        "NSE": "09:20",
        "MCX": "09:05"
    },
    "eod_squareoff_times": {
        "NSE": "15:20",
        "MCX": "23:20"
    }
}

# File paths
STATE_FILE = Path("trading_state.json")
LOG_FILE = Path("trading_bot.log")
ORDER_LOG_FILE = Path("orders.log")
MARKET_STATUS_LOG_FILE = Path("market_status.log")

# ============================================================================
# LOGGING SETUP
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

order_logger = logging.getLogger("order_logger")
order_logger.setLevel(logging.INFO)
order_logger.propagate = False
if not order_logger.handlers:
    order_handler = logging.FileHandler(ORDER_LOG_FILE, encoding='utf-8')
    order_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    order_logger.addHandler(order_handler)

market_status_logger = logging.getLogger("market_status_logger")
market_status_logger.setLevel(logging.INFO)
market_status_logger.propagate = False
if not market_status_logger.handlers:
    market_status_handler = logging.FileHandler(MARKET_STATUS_LOG_FILE, encoding='utf-8')
    market_status_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    market_status_logger.addHandler(market_status_handler)

# ============================================================================
# UPSTOX API CLIENT
# ============================================================================

class UpstoxClient:
    """Upstox API v2 Client for market data and orders"""
    
    def __init__(self, access_token, base_url):
        self.access_token = access_token
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        })
    
    def get_user_profile(self):
        """Get user profile information"""
        try:
            url = f"{self.base_url}/user/profile"
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
            return data.get('data', {})
        except Exception as e:
            logger.error(f"Error fetching user profile: {e}")
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
            logger.error(f"Error fetching historical candles: {e}")
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
            logger.error(f"Error fetching intraday candles: {e}")
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
                    logger.info(f" Order placed via {url}: {transaction_type} {quantity} of {instrument_key}")
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

        logger.error(
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
    """Main trading bot engine"""
    
    def __init__(self, config, client):
        self.config = config
        self.client = client
        self.positions = {}
        self.total_pnl = 0
        self.running = True
        self.analyzer = TechnicalAnalyzer()
        self.entry_warmup_done = False
        self.entry_warmup_timestamps = {}
        self.last_entry_candle_processed = {}
        self.last_position_eval_logged = {}
        self.eod_squareoff_done = {}
        
    def load_state(self):
        """Load saved trading state"""
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                    self.positions = state.get('positions', {})
                    self.total_pnl = state.get('total_pnl', 0)
                    self.eod_squareoff_done = state.get('eod_squareoff_done', {})
                    logger.info(f"STATE LOADED: {len(self.positions)} positions")
        except Exception as e:
            logger.warning(f"WARNING: Could not load state: {e}")
    
    def save_state(self):
        """Save current trading state"""
        try:
            state = {
                'positions': self.positions,
                'total_pnl': self.total_pnl,
                'eod_squareoff_done': self.eod_squareoff_done,
                'timestamp': datetime.now().isoformat()
            }
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"ERROR: Could not save state: {e}")

    def _ensure_position_fields(self, position):
        """Backfill position fields for older saved state compatibility."""
        entry_price = position.get('entry_price', 0)
        position_type = position.get('type')
        risk_percent = self.config['trailing_stop_loss_percent'] / 100

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

        if 'target_price' not in position and entry_price > 0:
            target_percent = self.config['target_percent'] / 100
            if position_type == 'BUY':
                position['target_price'] = entry_price * (1 + target_percent)
            elif position_type == 'SELL':
                position['target_price'] = entry_price * (1 - target_percent)

    def _log_order_event(self, script_name, action, side, price, reason, extra=""):
        order_logger.info(
            f"{script_name} | ACTION={action} | SIDE={side} | PRICE={price:.2f} | REASON={reason}"
            + (f" | {extra}" if extra else "")
        )

    def _log_order_failure(self, script_name, side, price, reason, error_text, endpoint=""):
        fail_extra = f"error={error_text}"
        if endpoint:
            fail_extra += f"; endpoint={endpoint}"
        order_logger.info(
            f"{script_name} | ACTION=ORDER_FAILED | SIDE={side} | PRICE={price:.2f} | REASON={reason} | {fail_extra}"
        )

    def _place_order_with_result(self, script_name, side, price, reason):
        order_token = self._get_order_token(script_name)
        order_qty = self._get_order_quantity(script_name)
        result = self.client.place_order(order_token, order_qty, side)
        if result and result.get('status') == 'success':
            return True, result

        error_text = (result or {}).get('error', 'Unknown error')
        endpoint = (result or {}).get('endpoint', '')
        logger.error(
            f"ORDER FAILED: {script_name} {side} qty={order_qty} @ Rs{price:.2f} | reason={reason} | error={error_text}"
        )
        self._log_order_failure(script_name, side, price, reason, error_text, endpoint)
        return False, result

    def _get_order_token(self, script_name):
        """Get the order token for placing orders (FUTURES/COMMODITIES)"""
        order_tokens = self.config.get('order_tokens', {})
        return order_tokens.get(script_name, self.config['scripts'].get(script_name, ''))

    def _get_order_quantity(self, script_name):
        """Get exchange quantity as lots multiplied by contract lot size."""
        lots = int(self.config.get('quantity', 1))
        lot_size = int(self.config.get('lot_sizes', {}).get(script_name, 1))
        return max(1, lots * lot_size)

    @staticmethod
    def _calculate_ob_percent(entry_price, stop_loss):
        if entry_price is None or stop_loss is None or entry_price <= 0:
            return 0.0
        return abs((entry_price - stop_loss) / entry_price) * 100

    def _get_min_ob_percent(self, script_name):
        return float(self.config.get('min_ob_percent_by_script', {}).get(script_name, 0.0))

    def _now_ist(self):
        return datetime.now(ZoneInfo("Asia/Kolkata"))

    def _script_segment(self, script_name):
        segment_scripts = self.config.get('segment_scripts', {})
        for segment, scripts in segment_scripts.items():
            if script_name in scripts:
                return segment
        return None

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

            logger.info(f"EOD: Square-off check for {segment} at {now_ist.strftime('%H:%M:%S')}")
            any_closed = False

            for script_name in scripts:
                if script_name not in self.positions:
                    continue

                position = self.positions[script_name]
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
                    "EOD_SQUAREOFF"
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
                        f"order_id={order_id}"
                    )
                )
                del self.positions[script_name]
                any_closed = True

            remaining = [s for s in scripts if s in self.positions]
            if not remaining:
                self.eod_squareoff_done[segment] = today_text
                logger.info(f"EOD: {segment} square-off completed for {today_text}")
                self.save_state()
            elif any_closed:
                self.save_state()
                logger.warning(f"EOD: {segment} square-off partial. Remaining: {remaining}")

    def _favorable_move_percent(self, position_type, entry_price, current_price):
        if position_type == 'BUY':
            return ((current_price - entry_price) / entry_price) * 100
        return ((entry_price - current_price) / entry_price) * 100

    def _calculate_stepped_sl(self, position_type, entry_price, steps):
        step_percent = self.config['trail_step_percent'] / 100
        if position_type == 'BUY':
            return entry_price * (1 + step_percent * steps)
        return entry_price * (1 - step_percent * steps)

    def _get_entry_swing_sl(self, df, entry_candle_timestamp, side):
        """Return swing-based SL from 5-minute candles before entry candle.

        BUY  -> most recent swing low before entry candle.
        SELL -> most recent swing high before entry candle.
        Fallback -> previous candle low/high.
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

        entry_idx = int(eligible.index[-1])
        prev_idx = entry_idx - 1
        if prev_idx < 0:
            return None

        pivot_indices = []
        search_end = prev_idx
        if search_end >= 2:
            for idx in range(1, search_end):
                prev_row = working.iloc[idx - 1]
                row = working.iloc[idx]
                next_row = working.iloc[idx + 1]

                if side == 'BUY' and row['low'] < prev_row['low'] and row['low'] < next_row['low']:
                    pivot_indices.append(idx)
                elif side == 'SELL' and row['high'] > prev_row['high'] and row['high'] > next_row['high']:
                    pivot_indices.append(idx)

        if pivot_indices:
            pivot_idx = pivot_indices[-1]
            return float(working.iloc[pivot_idx]['low'] if side == 'BUY' else working.iloc[pivot_idx]['high'])

        prev_row = working.iloc[prev_idx]
        return float(prev_row['low'] if side == 'BUY' else prev_row['high'])

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
        - For every extra 0.5% favorable move, SL moves by +0.5% (BUY) / -0.5% (SELL)
        """
        self._ensure_position_fields(position)
        entry_price = position['entry_price']
        position_type = position['type']

        initial_sl = position.get('initial_sl', position.get('stop_loss', entry_price))
        if entry_price > 0:
            risk_percent = abs((entry_price - initial_sl) / entry_price) * 100
        else:
            risk_percent = 0
        if risk_percent <= 0:
            risk_percent = self.config['trailing_stop_loss_percent']

        step_percent = self.config['trail_step_percent']
        favorable_move = self._favorable_move_percent(position_type, entry_price, current_price)

        if favorable_move < risk_percent:
            return

        if not position['breakeven_done']:
            position['stop_loss'] = entry_price
            position['breakeven_done'] = True
            logger.info(f"INFO: {script_name}: 1:1 reached. SL moved to cost @ Rs{entry_price:.2f}")

        extra_move = max(0.0, favorable_move - risk_percent)
        new_steps = int(extra_move // step_percent)

        if new_steps > position['trail_steps_locked']:
            position['trail_steps_locked'] = new_steps
            stepped_sl = self._calculate_stepped_sl(position_type, entry_price, new_steps)

            if position_type == 'BUY':
                position['stop_loss'] = max(position['stop_loss'], stepped_sl)
            else:
                position['stop_loss'] = min(position['stop_loss'], stepped_sl)

            logger.info(
                f"UPDATE: {script_name}: Trailing SL updated to Rs{position['stop_loss']:.2f} "
                f"(favorable move: {favorable_move:.2f}%, steps: {new_steps})"
            )

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
            
            logger.info(
                f" {script_name}: {len(df)} candles ({self.config.get('signal_interval', '1minute')}) "
                f"| Latest: Rs{df['close'].iloc[-1]:.2f}"
            )
            return df
            
        except Exception as e:
            logger.error(f"ERROR: Error fetching data for {script_name}: {e}")
            return None
    
    def process_script(self, script_name, instrument_key):
        """Process a single script for trading signals"""
        try:
            # Fetch market data
            df = self.fetch_market_data(script_name, instrument_key)
            if df is None or len(df) < self.config['ema_long']:
                logger.warning(f"WARNING: Insufficient data for {script_name}")
                return None
            
            # Calculate technical indicators
            df = self.analyzer.calculate_signals(
                df, 
                self.config['ema_short'], 
                self.config['ema_long']
            )
            
            if df is None:
                return None
            
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
            else:
                closed_signal = signal
                closed_crossover = False
                closed_ema_short = ema_short
                closed_ema_long = ema_long
                closed_timestamp = None
            
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
                'instrument_key': instrument_key,
                'current_price': current_price,
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
                'entry_candle_timestamp': closed_timestamp,
                'df': df
            }
            
        except Exception as e:
            logger.error(f"ERROR: Error processing {script_name}: {e}")
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

                market_status_logger.info(
                    f"{script_name} | EMA={data['ema_short']:.2f}/{data['ema_long']:.2f} | "
                    f"Status={data['signal_status']} | Timestamp={signal_time_text}"
                )
        
        print("="*110)
        print(f"{Fore.YELLOW}Total P&L: Rs{self.total_pnl:.2f}{Style.RESET_ALL}")
        print(f"{Fore.YELLOW}Active Positions: {len(self.positions)}{Style.RESET_ALL}")
        if self.positions:
            for script, pos in self.positions.items():
                sl = pos.get('stop_loss', pos.get('entry_price', 0))
                print(f"   - {script}: {pos['type']} @ Rs{pos['entry_price']:.2f} | SL: Rs{sl:.2f}")
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
            crossover = data['crossover']
            instrument_key = data['instrument_key']
            confirmed_signal = data.get('entry_signal', signal)
            confirmed_crossover = data.get('entry_crossover', crossover)
            confirmed_candle_timestamp = data.get('entry_candle_timestamp')
            
            # Check if we have an open position
            if script_name in self.positions:
                position = self.positions[script_name]
                self._ensure_position_fields(position)

                confirmed_time_text = confirmed_candle_timestamp.isoformat() if hasattr(confirmed_candle_timestamp, 'isoformat') else 'NA'
                last_eval_ts = self.last_position_eval_logged.get(script_name)
                if confirmed_time_text != 'NA' and last_eval_ts != confirmed_time_text:
                    logger.info(
                        f"VERIFY: {script_name} open={position['type']} | entry={position['entry_price']:.2f} | "
                        f"closed_signal={confirmed_signal} | closed_crossover={bool(confirmed_crossover)} | "
                        f"closed_time={confirmed_time_text}"
                    )
                    self.last_position_eval_logged[script_name] = confirmed_time_text

                # Update stepped trailing SL as per strategy
                self._update_position_sl(script_name, position, current_price)

                # Stop loss check
                stop_loss = position['stop_loss']
                if position['type'] == 'BUY' and current_price <= stop_loss:
                    logger.warning(
                        f"ALERT: SL hit for {script_name}. Closing BUY @ Rs{current_price:.2f} "
                        f"(SL: Rs{stop_loss:.2f})"
                    )
                    success, order_result = self._place_order_with_result(
                        script_name, "SELL", current_price, "STOP_LOSS_HIT"
                    )
                    if not success:
                        continue
                    order_id = (order_result.get('data') or {}).get('order_id', 'NA')
                    self._log_order_event(
                        script_name,
                        action="EXIT",
                        side="SELL",
                        price=current_price,
                        reason="STOP_LOSS_HIT",
                        extra=f"entry={position['entry_price']:.2f}; sl={stop_loss:.2f}; order_id={order_id}"
                    )
                    del self.positions[script_name]
                    self.save_state()
                    continue

                if position['type'] == 'SELL' and current_price >= stop_loss:
                    logger.warning(
                        f"ALERT: SL hit for {script_name}. Closing SELL @ Rs{current_price:.2f} "
                        f"(SL: Rs{stop_loss:.2f})"
                    )
                    success, order_result = self._place_order_with_result(
                        script_name, "BUY", current_price, "STOP_LOSS_HIT"
                    )
                    if not success:
                        continue
                    order_id = (order_result.get('data') or {}).get('order_id', 'NA')
                    self._log_order_event(
                        script_name,
                        action="EXIT",
                        side="BUY",
                        price=current_price,
                        reason="STOP_LOSS_HIT",
                        extra=f"entry={position['entry_price']:.2f}; sl={stop_loss:.2f}; order_id={order_id}"
                    )
                    del self.positions[script_name]
                    self.save_state()
                    continue

                # Target check
                favorable_move = self._favorable_move_percent(position['type'], position['entry_price'], current_price)
                if favorable_move >= self.config['target_percent']:
                    logger.info(
                        f" Target hit for {script_name}. Closing {position['type']} @ Rs{current_price:.2f} "
                        f"(move: {favorable_move:.2f}%)"
                    )
                    exit_side = "SELL" if position['type'] == 'BUY' else "BUY"
                    success, order_result = self._place_order_with_result(
                        script_name, exit_side, current_price, "TARGET_HIT"
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
                        extra=f"entry={position['entry_price']:.2f}; target={position.get('target_price', 0):.2f}; order_id={order_id}"
                    )
                    del self.positions[script_name]
                    self.save_state()
                    continue
                
                # Exit on opposite signal crossover
                if (position['type'] == 'BUY' and confirmed_signal == -1 and confirmed_crossover) or \
                   (position['type'] == 'SELL' and confirmed_signal == 1 and confirmed_crossover):
                    logger.info(
                        f"EXIT: Confirmed crossover exit for {script_name}. Closing {position['type']} position "
                        f"(signal_time={confirmed_time_text})."
                    )
                    exit_side = "SELL" if position['type'] == 'BUY' else "BUY"
                    success, order_result = self._place_order_with_result(
                        script_name, exit_side, current_price, "OPPOSITE_CROSSOVER"
                    )
                    if not success:
                        continue
                    order_id = (order_result.get('data') or {}).get('order_id', 'NA')
                    self._log_order_event(
                        script_name,
                        action="EXIT",
                        side=exit_side,
                        price=current_price,
                        reason="OPPOSITE_CROSSOVER",
                        extra=f"signal_time={confirmed_time_text}; order_id={order_id}"
                    )
                    del self.positions[script_name]
                    self.save_state()
            
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
                entry_price = current_price
                signal_df = data.get('df')

                if entry_crossover:
                    if entry_signal == 1:
                        initial_sl = self._get_entry_order_block_sl(signal_df, entry_candle_timestamp, 'BUY')
                        if initial_sl is None:
                            initial_sl = self._get_entry_swing_sl(signal_df, entry_candle_timestamp, 'BUY')
                        if initial_sl is None or initial_sl >= entry_price:
                            initial_sl = entry_price * (1 - self.config['trailing_stop_loss_percent'] / 100)
                        ob_percent = self._calculate_ob_percent(entry_price, initial_sl)
                        min_ob_percent = self._get_min_ob_percent(script_name)
                        if ob_percent < min_ob_percent:
                            logger.info(
                                f"SKIP: {script_name} BUY ignored due to low OB% "
                                f"(ob_pct={ob_percent:.2f}% < min_ob_pct={min_ob_percent:.2f}%)"
                            )
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            continue
                        logger.info(f"BUY signal for {script_name} at {entry_price:.2f}")
                        success, order_result = self._place_order_with_result(
                            script_name, "BUY", entry_price, "EMA_CROSSOVER"
                        )
                        if not success:
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            continue
                        target_price = entry_price * (1 + self.config['target_percent'] / 100)
                        signal_timestamp = entry_candle_timestamp
                        signal_timestamp_str = signal_timestamp.isoformat() if signal_timestamp is not None else datetime.now().isoformat()
                        order_id = (order_result.get('data') or {}).get('order_id', 'NA')
                        self.positions[script_name] = {
                            'type': 'BUY',
                            'entry_price': entry_price,
                            'entry_time': datetime.now().isoformat(),
                            'signal_time': signal_timestamp_str,
                            'signal_ema_short': entry_ema_short,
                            'signal_ema_long': entry_ema_long,
                            'ob_percent': ob_percent,
                            'initial_sl': initial_sl,
                            'stop_loss': initial_sl,
                            'target_price': target_price,
                            'trail_steps_locked': 0,
                            'breakeven_done': False
                        }
                        logger.info(f" {script_name}: Initial SL set @ Rs{initial_sl:.2f}")
                        self._log_order_event(
                            script_name,
                            action="ENTRY",
                            side="BUY",
                            price=entry_price,
                            reason="EMA_CROSSOVER",
                            extra=(
                                f"sl={initial_sl:.2f}; target={target_price:.2f}; "
                                f"ob_pct={ob_percent:.2f}; "
                                f"signal_time={signal_timestamp_str}; "
                                f"order_id={order_id}; "
                                f"ema{self.config['ema_short']}={entry_ema_short:.2f}; "
                                f"ema{self.config['ema_long']}={entry_ema_long:.2f}"
                            )
                        )
                        self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                        self.save_state()
                    
                    elif entry_signal == -1:
                        initial_sl = self._get_entry_order_block_sl(signal_df, entry_candle_timestamp, 'SELL')
                        if initial_sl is None:
                            initial_sl = self._get_entry_swing_sl(signal_df, entry_candle_timestamp, 'SELL')
                        if initial_sl is None or initial_sl <= entry_price:
                            initial_sl = entry_price * (1 + self.config['trailing_stop_loss_percent'] / 100)
                        ob_percent = self._calculate_ob_percent(entry_price, initial_sl)
                        min_ob_percent = self._get_min_ob_percent(script_name)
                        if ob_percent < min_ob_percent:
                            logger.info(
                                f"SKIP: {script_name} SELL ignored due to low OB% "
                                f"(ob_pct={ob_percent:.2f}% < min_ob_pct={min_ob_percent:.2f}%)"
                            )
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            continue
                        logger.info(f"SELL signal for {script_name} at {entry_price:.2f}")
                        success, order_result = self._place_order_with_result(
                            script_name, "SELL", entry_price, "EMA_CROSSOVER"
                        )
                        if not success:
                            self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                            continue
                        target_price = entry_price * (1 - self.config['target_percent'] / 100)
                        signal_timestamp = entry_candle_timestamp
                        signal_timestamp_str = signal_timestamp.isoformat() if signal_timestamp is not None else datetime.now().isoformat()
                        order_id = (order_result.get('data') or {}).get('order_id', 'NA')
                        self.positions[script_name] = {
                            'type': 'SELL',
                            'entry_price': entry_price,
                            'entry_time': datetime.now().isoformat(),
                            'signal_time': signal_timestamp_str,
                            'signal_ema_short': entry_ema_short,
                            'signal_ema_long': entry_ema_long,
                            'ob_percent': ob_percent,
                            'initial_sl': initial_sl,
                            'stop_loss': initial_sl,
                            'target_price': target_price,
                            'trail_steps_locked': 0,
                            'breakeven_done': False
                        }
                        logger.info(f" {script_name}: Initial SL set @ Rs{initial_sl:.2f}")
                        self._log_order_event(
                            script_name,
                            action="ENTRY",
                            side="SELL",
                            price=entry_price,
                            reason="EMA_CROSSOVER",
                            extra=(
                                f"sl={initial_sl:.2f}; target={target_price:.2f}; "
                                f"ob_pct={ob_percent:.2f}; "
                                f"signal_time={signal_timestamp_str}; "
                                f"order_id={order_id}; "
                                f"ema{self.config['ema_short']}={entry_ema_short:.2f}; "
                                f"ema{self.config['ema_long']}={entry_ema_long:.2f}"
                            )
                        )
                        self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                        self.save_state()
    
    def run(self):
        """Main trading loop"""
        logger.info("="*80)
        logger.info("STARTUP: Trading Bot Started")
        logger.info("="*80)
        
        # Load previous state
        self.load_state()
        
        # Verify credentials
        profile = self.client.get_user_profile()
        if profile:
            logger.info(f"CONNECTED: Connected as: {profile.get('user_name', 'Unknown')}")
        else:
            logger.error("ERROR: Failed to connect to Upstox API")
            return
        
        try:
            while self.running:
                try:
                    # Process all scripts
                    script_data = []
                    for script_name, instrument_key in self.config['scripts'].items():
                        data = self.process_script(script_name, instrument_key)
                        script_data.append(data)
                    
                    # Print status table
                    self.print_status_table(script_data)

                    allow_new_entries = self.entry_warmup_done

                    if not self.entry_warmup_done:
                        for data in script_data:
                            if data:
                                self.entry_warmup_timestamps[data['script_name']] = data.get('entry_candle_timestamp')
                        self.entry_warmup_done = True
                        logger.info("ENTRY WARMUP: Startup snapshot captured. New entries will trigger only on fresh crossover candles.")

                    latest_prices = {
                        data['script_name']: data['current_price']
                        for data in script_data
                        if data and data.get('current_price') is not None
                    }

                    now_ist = self._now_ist()
                    self._run_eod_squareoff(now_ist, latest_prices=latest_prices)
                    
                    # Execute trading logic
                    self.execute_trading_logic(script_data, allow_new_entries=allow_new_entries, now_ist=now_ist)
                    
                    # Check global stop loss
                    if self.total_pnl < -self.config['portfolio_stop_loss']:
                        logger.error(f" Portfolio stop loss hit! Total loss: Rs{self.total_pnl:.2f}")
                        logger.error(" Exiting all positions and stopping bot.")
                        self.positions.clear()
                        self.save_state()
                        self.running = False
                        break
                    
                    # Wait for next iteration
                    logger.info(f"Next update in {self.config['loop_interval']} seconds...\n")
                    time.sleep(self.config['loop_interval'])
                    
                except KeyboardInterrupt:
                    logger.info("\n Keyboard interrupt detected. Shutting down gracefully...")
                    self.running = False
                    break
                except Exception as e:
                    logger.error(f" Error in trading loop: {e}")
                    time.sleep(self.config['loop_interval'])
        
        finally:
            self.save_state()
            logger.info("="*80)
            logger.info(" Trading Bot Stopped")
            logger.info("="*80)

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main():
    """Main entry point"""
    print(f"{Fore.CYAN}")
    print("="*80)
    print("   MULTI-SCRIPT TRADING BOT v2.0")
    print("   EMA Crossover Strategy")
    print("   " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("="*80)
    print(f"{Style.RESET_ALL}")
    
    # Display public IP
    try:
        public_ip = requests.get('https://api.ipify.org', timeout=5).text
        print(f"{Fore.YELLOW} Public IP: {public_ip}{Style.RESET_ALL}\n")
    except:
        pass
    
    # Initialize client
    client = UpstoxClient(
        API_CONFIG['access_token'],
        API_CONFIG['base_url']
    )
    
    # Initialize and run bot
    bot = TradingBot(TRADING_CONFIG, client)
    bot.run()

if __name__ == "__main__":
    main()
