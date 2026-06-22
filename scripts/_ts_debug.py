import sys; sys.path.insert(0,'src/server/src')
from app.services.backtest_data import HistoricalDataClient, parse_candle_ts
from app.services.upstox_engine import INDEX_CONFIGS
from app.services.backtest_runner import _bar_close_ts, S7_ENTRY_START, S7_NO_ENTRY
from datetime import date, timedelta

data = HistoricalDataClient(username='AK07')
cfg = INDEX_CONFIGS['NIFTY']
end = date.today()
candles = data.fetch_5m(cfg.spot_instrument_key, end - timedelta(days=5), end)

print(f"S7_ENTRY_START={S7_ENTRY_START}  S7_NO_ENTRY={S7_NO_ENTRY}")
print()
if candles:
    for c in candles[:8]:
        raw = c['timestamp']
        parsed = parse_candle_ts(raw)
        bc = _bar_close_ts(c)
        in_window = S7_ENTRY_START <= bc.time() <= S7_NO_ENTRY
        print(f"raw={raw}  parsed={parsed}  bar_close={bc.time()}  in_window={in_window}")
