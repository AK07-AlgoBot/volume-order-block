"""AK07 — High-Velocity Volume Order Block Breakout Logic.

Translates the 100% volume compression and velocity status parameters 
into clear option buying signals (CE/PE entries).
"""

from __future__ import annotations
import numpy as np


def calculate_ema(prices: list[float], period: int) -> list[float]:
    """Calculates Exponential Moving Average (EMA)."""
    prices_arr = np.array(prices)
    alpha = 2.0 / (period + 1)
    ema = [prices_arr[0]]
    for price in prices_arr[1:]:
        ema.append(alpha * price + (1 - alpha) * ema[-1])
    return ema


def check_velocity_breakout(
    candles: list[dict], 
    sensitivity: int = 5
) -> dict:
    """Analyzes recent OHLCV bar structures for 100% volume block breakouts

    matching the TradingView Pine Script execution criteria.
    
    Args:
        candles: List of dicts containing 'open', 'high', 'low', 'close', 'volume'
        sensitivity: Period for fast EMA crossover logic.
    """
    if len(candles) < (sensitivity + 13 + 20):
        return {"status": "WAIT", "message": "Insufficient historical bar depth."}

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]
    
    # 1. Replicate EMA Crossovers
    length2 = sensitivity + 13
    ema1 = calculate_ema(closes, sensitivity)
    ema2 = calculate_ema(closes, length2)
    
    # Check if a crossover/crossunder occurred on the previous confirmed bar
    cross_up = (ema1[-2] > ema2[-2]) and (ema1[-3] <= ema2[-3])
    cross_dn = (ema1[-2] < ema2[-2]) and (ema1[-3] >= ema2[-3])
    
    # 2. Volume Velocity Filter (Is current volume above 20-period moving average?)
    recent_volumes = volumes[-20:]
    vol_ma = sum(recent_volumes) / len(recent_volumes)
    current_volume = volumes[-1]
    is_velocity = current_volume > vol_ma

    # Current reference metrics
    current_close = closes[-1]
    
    # Structural pivots from prior bars (exclude current candle — close cannot exceed its own high)
    pivot_highs = highs[-length2:-1]
    pivot_lows = lows[-length2:-1]
    if not pivot_highs or not pivot_lows:
        return {"status": "WAIT", "message": "Insufficient pivot window."}

    highest_pivot = max(pivot_highs)
    lowest_pivot = min(pivot_lows)
    
    # Calculate strike selection base (rounded to nearest 50 for Nifty index guidelines)
    atm_strike = round(current_close / 50) * 50

    # 3. Decision Matrix Evaluation
    # Bullish Breakout Setup: Price clears the high pivot barrier under high velocity
    if current_close > highest_pivot and is_velocity:
        return {
            "status": "BUY_CE",
            "strike": atm_strike,
            "underlying_ltp": current_close,
            "trigger_level": highest_pivot,
            "signal_type": "💥 VELOCITY SQUEEZE BREAKOUT",
            "action_plan": f"Buy ATM {atm_strike} CE. Momentum is expanding above overhead Call Wall."
        }
        
    # Bearish Breakdown Setup: Price drops beneath the low pivot barrier under high velocity
    elif current_close < lowest_pivot and is_velocity:
        return {
            "status": "BUY_PE",
            "strike": atm_strike,
            "underlying_ltp": current_close,
            "trigger_level": lowest_pivot,
            "signal_type": "💥 VELOCITY BREAKDOWN",
            "action_plan": f"Buy ATM {atm_strike} PE. Sellers have successfully flushed the Put Floor."
        }
        
    # Choppy consolidation states
    else:
        return {
            "status": "HOLD",
            "underlying_ltp": current_close,
            "signal_type": "STAGNANT / BALANCE CORRIDOR",
            "action_plan": "Maintain capital safety. Price is oscillating inside the order block walls."
        }

# --- Quick Test Validation ---
if __name__ == "__main__":
    # Mocking sample market bars showing a high velocity upside breakthrough
    mock_bars = [{"open": 24000, "high": 24050, "low": 23980, "close": 24010, "volume": 1000} for _ in range(40)]
    # Simulate a sudden massive breakout bar
    mock_bars.append({"open": 24010, "high": 24120, "low": 24005, "close": 24115, "volume": 5500})
    
    signal = check_velocity_breakout(mock_bars)
    print(f"Engine Output:\n{signal}")