"""Debug why S7v2 fires 0 trades — show per-gate failure counts."""
import sys, os
sys.path.insert(0, "src/server/src")
os.environ.update({"AK07_MOCK": "0", "AK07_MOCK_MODE": "0", "AK07_PAPER_TRADING": "1"})

from datetime import date, timedelta, time as dtime
from app.services.backtest_data import HistoricalDataClient, parse_candle_ts
from app.services.backtest_runner import build_blr_context, _bar_close_ts, S7_OR_END, S7_SESSION_START, S7_ENTRY_START, S7_NO_ENTRY
from app.services.s7_vwap_breakout_engine import (
    atr as s7_atr, vwap_series, ema_series, _vol_avg,
    MIN_OR_ATR_RATIO, MAX_OR_ATR_RATIO, VOL_MULTIPLIER, VOL_AVG_BARS,
    BODY_RATIO_MIN, MAX_EXTENSION_ATR, EMA_FAST, EMA_SLOW,
    ATR_PERIOD, SL_BUFFER,
)
from app.services.upstox_engine import INDEX_CONFIGS

end = date.today()
start = end - timedelta(days=90)
fetch_start = start - timedelta(days=45)

data = HistoricalDataClient(username="AK07")

gate_fails = {
    "or_range_bad": 0,
    "day_review_bad": 0,
    "insufficient_bars": 0,
    "no_or_break": 0,
    "atr_none": 0,
    "or_range_in_signal": 0,
    "ema_not_ready": 0,
    "body_ratio": 0,
    "volume": 0,
    "not_directional": 0,
    "extension": 0,
    "vwap_slope": 0,
    "not_above_vwap": 0,
    "ALL_PASS": 0,
}

total_candidate_bars = 0

for code in ["NIFTY", "BANKNIFTY", "SENSEX"]:
    cfg = INDEX_CONFIGS[code]
    candles_5m = data.fetch_5m(cfg.spot_instrument_key, fetch_start, end)
    daily = data.fetch_daily(cfg.spot_instrument_key, fetch_start, end)
    days = sorted(HistoricalDataClient.trading_days(candles_5m, start, end))
    
    for day in days:
        session = HistoricalDataClient.session_5m(candles_5m, day)
        if len(session) < 6:
            continue
        prev_ohlc = HistoricalDataClient.prior_session_ohlc(daily, day)
        if not prev_ohlc:
            continue
        blr = build_blr_context(prev_ohlc, session, code)
        if not blr:
            continue
        
        # Build OR
        or_bars = [c for c in session if S7_SESSION_START <= parse_candle_ts(c["timestamp"]).time() < S7_OR_END]
        if not or_bars:
            continue
        or_high = max(float(c["high"]) for c in or_bars)
        or_low = min(float(c["low"]) for c in or_bars)
        or_range = or_high - or_low
        
        # Don't check ATR at OR time (too few bars)
        
        if blr.day_review not in ("LONG", "SHORT"):
            gate_fails["day_review_bad"] += 1
            continue
        
        for idx, candle in enumerate(session):
            bar_close = _bar_close_ts(candle)
            if not (S7_ENTRY_START <= bar_close.time() <= S7_NO_ENTRY):
                continue
            closed = session[:idx+1]
            if len(closed) < max(ATR_PERIOD, EMA_SLOW) + 2:
                gate_fails["insufficient_bars"] += 1
                continue
            
            close = float(candle["close"])
            open_ = float(candle["open"])
            high = float(candle["high"])
            low = float(candle["low"])
            
            # Is it a breakout bar at all?
            if blr.day_review == "LONG" and close <= or_high:
                gate_fails["no_or_break"] += 1
                continue
            if blr.day_review == "SHORT" and close >= or_low:
                gate_fails["no_or_break"] += 1
                continue
            
            total_candidate_bars += 1
            
            atr_v = s7_atr(closed)
            if not atr_v:
                gate_fails["atr_none"] += 1
                continue
            
            if or_range < MIN_OR_ATR_RATIO * atr_v or or_range > MAX_OR_ATR_RATIO * atr_v:
                gate_fails["or_range_in_signal"] += 1
                continue
            
            ema9 = ema_series(closed, EMA_FAST)
            ema21 = ema_series(closed, EMA_SLOW)
            if not ema9 or not ema21:
                gate_fails["ema_not_ready"] += 1
                continue
            
            avg_vol = _vol_avg(closed, VOL_AVG_BARS)
            candle_vol = float(candle.get("volume") or 0)
            vol_ok = avg_vol <= 0 or candle_vol >= VOL_MULTIPLIER * avg_vol
            
            vwap_vals = vwap_series(closed)
            current_vwap = vwap_vals[-1]
            body = abs(close - open_)
            cr = max(high - low, 0.01)
            body_ratio = body / cr
            
            if blr.day_review == "LONG":
                if body_ratio < BODY_RATIO_MIN:
                    gate_fails["body_ratio"] += 1; continue
                if not vol_ok:
                    gate_fails["volume"] += 1; continue
                if close <= open_:
                    gate_fails["not_directional"] += 1; continue
                if close > or_high + MAX_EXTENSION_ATR * atr_v:
                    gate_fails["extension"] += 1; continue
                if current_vwap <= 0 or not (vwap_vals[-1] > vwap_vals[-1-min(4, len(vwap_vals)-1)]):
                    gate_fails["vwap_slope"] += 1; continue
                if close <= current_vwap:
                    gate_fails["not_above_vwap"] += 1; continue
                if ema9[-1] <= ema21[-1]:
                    gate_fails["ema_not_ready"] += 1; continue
                gate_fails["ALL_PASS"] += 1
            else:
                if body_ratio < BODY_RATIO_MIN:
                    gate_fails["body_ratio"] += 1; continue
                if not vol_ok:
                    gate_fails["volume"] += 1; continue
                if close >= open_:
                    gate_fails["not_directional"] += 1; continue
                if close < or_low - MAX_EXTENSION_ATR * atr_v:
                    gate_fails["extension"] += 1; continue
                if not (vwap_vals[-1] < vwap_vals[-1-min(4, len(vwap_vals)-1)]):
                    gate_fails["vwap_slope"] += 1; continue
                if close >= current_vwap:
                    gate_fails["not_above_vwap"] += 1; continue
                if ema9[-1] >= ema21[-1]:
                    gate_fails["ema_not_ready"] += 1; continue
                gate_fails["ALL_PASS"] += 1

print(f"\nCandidate bars (post OR-break): {total_candidate_bars}")
print("\nGate failure breakdown (first failing gate per bar):")
for gate, count in sorted(gate_fails.items(), key=lambda x: -x[1]):
    pct = 100*count/max(total_candidate_bars,1)
    print(f"  {gate:<25} {count:>5}  ({pct:.1f}%)")
