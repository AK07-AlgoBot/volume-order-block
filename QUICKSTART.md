# ⚡ Quick Start Guide

Get your trading bot running in **5 minutes**!

## Step 1: Install Dependencies ⏱️ 1 min
```powershell
pip install -r requirements.txt
```

## Step 2: Upstox credentials ⏱️ 1 min

**Option A (recommended):** Start the stack (`start.bat`), open the dashboard at `http://localhost:5173`, scroll to **Upstox credentials**, paste your daily access token (and key/secret if you use them), then save.

**Option B:** Copy `upstox_credentials.example.json` to `upstox_credentials.json` in the project root and edit the values (this file is gitignored).

## Step 3: Run the Bot + UI ⏱️ 30 sec

**Option A: Single launcher (recommended)**
```powershell
# Double-click or run:
start.bat
```

This starts everything:

- Dashboard API (`http://localhost:8000`)
- Dashboard UI (`http://localhost:5173`)
- Trading bot (waits and retries until a valid token is on file or in env)

Bot only (no dashboard):

```powershell
start.bat -BotOnly
```

**Option B: Using command line**
```powershell
python trading_bot.py
```

## Step 4: Monitor Output ⏱️ 2 min

You'll see colorful output like this:

```
==================================================================
🤖 MULTI-SCRIPT TRADING BOT v2.0
📈 EMA Crossover Strategy
📅 2026-03-04 10:30:00
==================================================================
🌐 Public IP: 123.45.67.89

👤 Connected as: YourName

📊 NIFTY: 88 candles | Latest: ₹24515.00
📊 BANKNIFTY: 88 candles | Latest: ₹58994.00

==================================================================
SCRIPT          PRICE        EMA5         EMA18        SIGNAL       STATUS     
==================================================================
NIFTY           24515.00     24505.53     24599.75     SELL                    
BANKNIFTY       58994.00     59018.96     59304.59     SELL                    
FINNIFTY        22890.30     22895.45     22950.67     BUY          🔔 CROSSOVER  
SENSEX          73256.45     73240.12     73350.89     SELL                    
==================================================================
💰 Total P&L: ₹0.00
📈 Active Positions: 0
==================================================================

⏳ Next update in 10 seconds...
```

## Step 5: Stop the Bot ⏱️ 5 sec

Press `Ctrl+C` to stop gracefully. The bot will:
- Save current state
- Log the shutdown
- Exit cleanly

---

## 🎯 What Each Color Means

- 🟢 **Green (BUY)**: EMA5 > EMA18 - Bullish trend
- 🔴 **Red (SELL)**: EMA5 < EMA18 - Bearish trend  
- 🟡 **Yellow (NEUTRAL)**: No clear trend
- 🔔 **CROSSOVER**: Signal just changed - Entry opportunity!

---

## 📊 Understanding the Display

### Top Section
- Shows connection status
- Your public IP
- Logged-in username

### Data Fetch Logs
```
📊 NIFTY: 88 candles | Latest: ₹24515.00
```
- Number of candles fetched
- Latest closing price

### Status Table
```
SCRIPT          PRICE        EMA5         EMA18        SIGNAL       STATUS     
NIFTY           24515.00     24505.53     24599.75     SELL                    
```
- **SCRIPT**: Index name
- **PRICE**: Current price
- **EMA5**: 5-period EMA value
- **EMA18**: 18-period EMA value
- **SIGNAL**: BUY/SELL/NEUTRAL
- **STATUS**: CROSSOVER alert

### Bottom Section
```
💰 Total P&L: ₹0.00
📈 Active Positions: 0
```
- Current profit/loss
- Number of open positions
- Position details (if any)

---

## ⚙️ Quick Configuration

Want to change settings? Edit these in `trading_bot.py`:

### Change Update Frequency
```python
"loop_interval": 10,  # Change to 5, 30, 60, etc. (seconds)
```

### Change Timeframe
```python
"interval": "5minute",  # Try: "1minute", "30minute", "day"
```

### Adjust EMA Periods
```python
"ema_short": 5,   # Try: 9, 12, etc.
"ema_long": 18,   # Try: 21, 26, 50, etc.
```

### Add More Indices
```python
"scripts": {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "MIDCAP": "NSE_INDEX|NIFTY MIDCAP 100",  # Add this
}
```

---

## 🛡️ Safety First!

### Before Going Live:

1. ✅ Run bot for 1-2 days in paper trading mode
2. ✅ Review logs in `trading_bot.log`
3. ✅ Verify signals are accurate
4. ✅ Test stop loss triggers
5. ✅ Understand all configurations

### Currently in Paper Trading Mode

All order placement code is **commented out**:
```python
# self.client.place_order(...)  # ← Not executed
```

This means:
- ✅ Bot monitors market
- ✅ Generates signals
- ✅ Logs everything
- ❌ Does NOT place real orders

---

## 📝 Log Files

### `trading_bot.log`
Detailed logs with timestamps:
```
2026-03-04 10:30:00 - INFO - 🚀 Trading Bot Started
2026-03-04 10:30:01 - INFO - 👤 Connected as: YourName
2026-03-04 10:30:02 - INFO - 📊 NIFTY: 88 candles | Latest: ₹24515.00
2026-03-04 10:30:03 - INFO - 🟢 BUY signal for FINNIFTY at ₹22890.30
```

### `trading_state.json`
Current positions (auto-saved):
```json
{
  "positions": {
    "FINNIFTY": {
      "type": "BUY",
      "entry_price": 22890.30,
      "entry_time": "2026-03-04T10:30:03"
    }
  },
  "total_pnl": 0,
  "timestamp": "2026-03-04T10:30:03"
}
```

---

## ❓ Common Issues

### "Module not found" error
```powershell
pip install -r requirements.txt
```

### "Invalid token" error
- Get new token from Upstox
- Update via dashboard **Upstox credentials** or edit `upstox_credentials.json`

### "No data fetched" error
- Check if market is open (9:15 AM - 3:30 PM IST)
- Verify internet connection
- Ensure historical data access in Upstox

### Bot keeps restarting?
- Check `trading_bot.log` for errors
- Verify token hasn't expired
- Ensure sufficient API quota

---

## 🎓 Next Steps

1. **Monitor for a day** - Let it run and observe signals
2. **Review logs** - Check `trading_bot.log` for patterns
3. **Adjust parameters** - Fine-tune EMA periods if needed
4. **Backtest strategy** - Use historical data to validate
5. **Enable live trading** - Only when confident!

---

## 📞 Need Help?

1. Check `trading_bot.log` for errors
2. Read the full [README.md](README.md)
3. Review [CHANGELOG.md](CHANGELOG.md)

---

**🚀 Happy Trading!**

Remember: This is paper trading mode. No real orders will be placed until you enable them.
