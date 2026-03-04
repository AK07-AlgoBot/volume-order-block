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
    "access_token": "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI2RUJBVTQiLCJqdGkiOiI2OWE3YTYyZDJjZWVlZDc1MjAwNGJmOTkiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlhdCI6MTc3MjU5NDczMywiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxNzcyNjYxNjAwfQ.f-XDPUS3a8q5ycehtlonJiGvIBtRYxX0KWxl88qHxpw",
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
    "loop_interval": 10,  # seconds between each check
    "quantity": 1  # Quantity per order
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
        try:
            url = "https://api-hft.upstox.com/v2/order/place"
            payload = {
                "quantity": quantity,
                "product": "I",  # Intraday for MCX, use "I" for futures
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
            
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            
            if data.get('status') == 'success':
                logger.info(f" Order placed: {transaction_type} {quantity} of {instrument_key}")
                return data.get('data')
            else:
                logger.error(f"ERROR: Order failed: {data}")
                return None
        except Exception as e:
            logger.error(f"ERROR: Error placing order: {e}")
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
        
    def load_state(self):
        """Load saved trading state"""
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                    self.positions = state.get('positions', {})
                    self.total_pnl = state.get('total_pnl', 0)
                    logger.info(f"STATE LOADED: {len(self.positions)} positions")
        except Exception as e:
            logger.warning(f"WARNING: Could not load state: {e}")
    
    def save_state(self):
        """Save current trading state"""
        try:
            state = {
                'positions': self.positions,
                'total_pnl': self.total_pnl,
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

    def _get_order_token(self, script_name):
        """Get the order token for placing orders (FUTURES/COMMODITIES)"""
        order_tokens = self.config.get('order_tokens', {})
        return order_tokens.get(script_name, self.config['scripts'].get(script_name, ''))

    def _favorable_move_percent(self, position_type, entry_price, current_price):
        if position_type == 'BUY':
            return ((current_price - entry_price) / entry_price) * 100
        return ((entry_price - current_price) / entry_price) * 100

    def _calculate_stepped_sl(self, position_type, entry_price, steps):
        step_percent = self.config['trail_step_percent'] / 100
        if position_type == 'BUY':
            return entry_price * (1 + step_percent * steps)
        return entry_price * (1 - step_percent * steps)

    def _update_position_sl(self, script_name, position, current_price):
        """
        Rule:
        - Initial risk = trailing_stop_loss_percent (default 1%)
        - At 1:1 (favorable move >= risk%), SL moves to cost
        - For every extra 0.5% favorable move, SL moves by +0.5% (BUY) / -0.5% (SELL)
        """
        self._ensure_position_fields(position)
        entry_price = position['entry_price']
        position_type = position['type']

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
    
    def execute_trading_logic(self, script_data, allow_new_entries=True):
        """Execute trading logic based on signals"""
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

                # Update stepped trailing SL as per strategy
                self._update_position_sl(script_name, position, current_price)

                # Stop loss check
                stop_loss = position['stop_loss']
                if position['type'] == 'BUY' and current_price <= stop_loss:
                    logger.warning(
                        f"ALERT: SL hit for {script_name}. Closing BUY @ Rs{current_price:.2f} "
                        f"(SL: Rs{stop_loss:.2f})"
                    )
                    self._log_order_event(
                        script_name,
                        action="EXIT",
                        side="SELL",
                        price=current_price,
                        reason="STOP_LOSS_HIT",
                        extra=f"entry={position['entry_price']:.2f}; sl={stop_loss:.2f}"
                    )
                    order_token = self._get_order_token(script_name)
                    self.client.place_order(order_token, self.config['quantity'], "SELL")
                    del self.positions[script_name]
                    self.save_state()
                    continue

                if position['type'] == 'SELL' and current_price >= stop_loss:
                    logger.warning(
                        f"ALERT: SL hit for {script_name}. Closing SELL @ Rs{current_price:.2f} "
                        f"(SL: Rs{stop_loss:.2f})"
                    )
                    self._log_order_event(
                        script_name,
                        action="EXIT",
                        side="BUY",
                        price=current_price,
                        reason="STOP_LOSS_HIT",
                        extra=f"entry={position['entry_price']:.2f}; sl={stop_loss:.2f}"
                    )
                    order_token = self._get_order_token(script_name)
                    self.client.place_order(order_token, self.config['quantity'], "BUY")
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
                    self._log_order_event(
                        script_name,
                        action="EXIT",
                        side=exit_side,
                        price=current_price,
                        reason="TARGET_HIT",
                        extra=f"entry={position['entry_price']:.2f}; target={position.get('target_price', 0):.2f}"
                    )
                    order_token = self._get_order_token(script_name)
                    exit_side = "SELL" if position['type'] == 'BUY' else "BUY"
                    self.client.place_order(order_token, self.config['quantity'], exit_side)
                    del self.positions[script_name]
                    self.save_state()
                    continue
                
                # Exit on opposite signal crossover
                if (position['type'] == 'BUY' and confirmed_signal == -1 and confirmed_crossover) or \
                   (position['type'] == 'SELL' and confirmed_signal == 1 and confirmed_crossover):
                    if hasattr(confirmed_candle_timestamp, 'isoformat'):
                        confirmed_time_text = confirmed_candle_timestamp.isoformat()
                    else:
                        confirmed_time_text = 'NA'
                    logger.info(
                        f"EXIT: Confirmed crossover exit for {script_name}. Closing {position['type']} position "
                        f"(signal_time={confirmed_time_text})."
                    )
                    exit_side = "SELL" if position['type'] == 'BUY' else "BUY"
                    self._log_order_event(
                        script_name,
                        action="EXIT",
                        side=exit_side,
                        price=current_price,
                        reason="OPPOSITE_CROSSOVER",
                        extra=f"signal_time={confirmed_time_text}"
                    )
                    order_token = self._get_order_token(script_name)
                    opposite_type = "SELL" if position['type'] == 'BUY' else "BUY"
                    self.client.place_order(order_token, self.config['quantity'], opposite_type)
                    del self.positions[script_name]
                    self.save_state()
            
            else:
                # Enter new position on crossover
                if not allow_new_entries:
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

                if entry_crossover:
                    if entry_signal == 1:
                        logger.info(f"BUY signal for {script_name} at {entry_price:.2f}")
                        order_token = self._get_order_token(script_name)
                        self.client.place_order(order_token, self.config['quantity'], "BUY")
                        initial_sl = entry_price * (1 - self.config['trailing_stop_loss_percent'] / 100)
                        target_price = entry_price * (1 + self.config['target_percent'] / 100)
                        signal_timestamp = entry_candle_timestamp
                        signal_timestamp_str = signal_timestamp.isoformat() if signal_timestamp is not None else datetime.now().isoformat()
                        self.positions[script_name] = {
                            'type': 'BUY',
                            'entry_price': entry_price,
                            'entry_time': datetime.now().isoformat(),
                            'signal_time': signal_timestamp_str,
                            'signal_ema_short': entry_ema_short,
                            'signal_ema_long': entry_ema_long,
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
                                f"signal_time={signal_timestamp_str}; "
                                f"ema{self.config['ema_short']}={entry_ema_short:.2f}; "
                                f"ema{self.config['ema_long']}={entry_ema_long:.2f}"
                            )
                        )
                        self.last_entry_candle_processed[script_name] = entry_candle_timestamp
                        self.save_state()
                    
                    elif entry_signal == -1:
                        logger.info(f"SELL signal for {script_name} at {entry_price:.2f}")
                        order_token = self._get_order_token(script_name)
                        self.client.place_order(order_token, self.config['quantity'], "SELL")
                        initial_sl = entry_price * (1 + self.config['trailing_stop_loss_percent'] / 100)
                        target_price = entry_price * (1 - self.config['target_percent'] / 100)
                        signal_timestamp = entry_candle_timestamp
                        signal_timestamp_str = signal_timestamp.isoformat() if signal_timestamp is not None else datetime.now().isoformat()
                        self.positions[script_name] = {
                            'type': 'SELL',
                            'entry_price': entry_price,
                            'entry_time': datetime.now().isoformat(),
                            'signal_time': signal_timestamp_str,
                            'signal_ema_short': entry_ema_short,
                            'signal_ema_long': entry_ema_long,
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
                                f"signal_time={signal_timestamp_str}; "
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
                    
                    # Execute trading logic
                    self.execute_trading_logic(script_data, allow_new_entries=allow_new_entries)
                    
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
