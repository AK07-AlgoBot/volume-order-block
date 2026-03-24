# Multi-Script Trading Bot v2.0

A professional-grade automated trading bot for Indian markets using Upstox API v2 with EMA crossover strategy.

## Features

✅ **Real-time Market Data**: Fetches historical + intraday 5-minute candles  
✅ **EMA Crossover Strategy**: 5-period and 18-period exponential moving averages  
✅ **Multi-Script Support**: NIFTY, BANKNIFTY, SENSEX, FINNIFTY  
✅ **Risk Management**: Portfolio stop loss & trailing stop loss per position  
✅ **State Persistence**: Saves and loads trading state across restarts  
✅ **Color-Coded Display**: Visual status table with buy/sell signals  
✅ **Comprehensive Logging**: Detailed logs of all activities  

## Requirements

- Python 3.8+
- Active Upstox Pro account
- Valid Upstox API access token

## Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Run the bot
python trading_bot.py
```

## Configuration

Edit the configuration section in `trading_bot.py`:

```python
TRADING_CONFIG = {
    "scripts": {
        "NIFTY": "NSE_INDEX|Nifty 50",
        "BANKNIFTY": "NSE_INDEX|Nifty Bank",
        # Add more scripts
    },
    "interval": "5minute",  # 1minute, 5minute, 30minute, day
    "ema_short": 5,
    "ema_long": 18,
    "portfolio_stop_loss": 10000,  # ₹10,000
    "trailing_stop_loss_percent": 1.0,  # 1%
    "loop_interval": 10  # seconds
}
```

## Trading Strategy

### Entry Signals
- **BUY**: When EMA(5) crosses above EMA(18)
- **SELL**: When EMA(5) crosses below EMA(18)

### Exit Conditions
1. Opposite crossover signal
2. Trailing stop loss hit (1% from entry)
3. Portfolio stop loss hit (₹10,000)

## Files Generated

- `trading_bot.log` - Detailed activity logs
- `trading_state.json` - Persistent trading state

## Safety Features

🛡️ **Paper Trading Mode**: Currently configured for testing (orders commented out)  
🛡️ **Stop Loss Protection**: Automatic position exit on adverse moves  
🛡️ **Portfolio Risk Management**: Global stop loss across all positions  
🛡️ **State Recovery**: Resumes positions after restart  

## Usage

```bash
# Start the bot
python trading_bot.py

# Stop with Ctrl+C (saves state automatically)
```

### Windows launchers

- `run_bot.bat`: starts bot + dashboard API + dashboard UI (recommended daily launcher)
- `run_bot.bat --bot-only` or `run_bot_only.bat`: starts only the trading bot

## API Token Update

Update your access token in the configuration:

```python
API_CONFIG = {
    "access_token": "your_new_token_here",
    "base_url": "https://api.upstox.com/v2"
}
```

## Production Deployment

To enable live trading, uncomment these lines:

```python
# In execute_trading_logic method
self.client.place_order(data['instrument_key'], 1, "BUY")
self.client.place_order(data['instrument_key'], 1, "SELL")
```

⚠️ **Warning**: Only enable live trading after thorough testing!

## Support

For issues or questions:
- Check logs in `trading_bot.log`
- Verify API token validity
- Ensure market hours for data availability

## License

MIT License - Use at your own risk. Trading involves financial risk.

---

**Disclaimer**: This software is for educational purposes. Past performance does not guarantee future results. Always test thoroughly before live trading.
Volume order block and EMA crossover

## OB% from Upstox (not `orders.log`)

To print **BUY and SELL** OB% / OB volume for all configured symbols using **live Upstox candles** (same logic as the bot):

```bash
python scripts/fetch_ob_snapshot.py
python scripts/fetch_ob_snapshot.py --json
python scripts/fetch_ob_snapshot.py --scripts CRUDE NIFTY --json
```
