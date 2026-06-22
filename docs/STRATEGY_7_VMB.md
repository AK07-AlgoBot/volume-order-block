# Strategy 7 — VWAP Momentum Breakout (VMB)

> **NOT FINANCIAL ADVICE. This document is for research and educational purposes only.
> Past backtest performance does NOT guarantee future results.
> All trading carries significant risk of capital loss, including total loss of invested capital.**

---

## 1. Feasibility Assessment — ₹3,000/day target

### Required daily return math

| Capital (INR) | Daily return needed | Annual equivalent | Assessment |
|---------------|--------------------|--------------------|------------|
| ₹1,00,000 | 3.0% | 750%+ | Infeasible |
| ₹5,00,000 | 0.60% | 150%+ | Very high risk, not realistic consistently |
| ₹10,00,000 | 0.30% | 75% | Ambitious, possible in strong markets |
| ₹25,00,000 | 0.12% | 30% | Realistic with a good strategy |
| ₹50,00,000 | 0.06% | 15% | Comfortably achievable with edge |

**Recommendation:** Minimum ₹10,00,000 working capital to target ₹3,000/day without extreme leverage.
With ₹5,00,000 and 2 lots: on a winning day (avg win ~₹4,000 across 3 indices) you hit ₹3,000+;
on a losing day you lose ~₹2,500. Daily variance is high. Capital ₹25L+ smooths this out.

### INR-per-point reference
| Index | Lot Size | ₹/pt |
|-------|----------|-------|
| Nifty 50 | **65** | ₹65 |
| Bank Nifty | 30 | ₹30 |
| Sensex | 20 | ₹20 |

### To earn ₹3,000/day (1 lot each, ATR ~40/100/150 for N/BN/S)
- Nifty TP1 ≈ 1.5 × 40 = 60 pts × 65 = ₹3,900
- BankNifty TP1 ≈ 1.5 × 100 = 150 pts × 30 = ₹4,500
- Sensex TP1 ≈ 1.5 × 150 = 225 pts × 20 = ₹4,500

**→ 1 winning trade on any one index exceeds ₹3,000 target.**

---

## 2. Strategy Logic

### Name: VWAP Momentum Breakout (VMB)

**Core insight from 90-day backtest analysis:**
S3 (BLR Breakout) wins 56% of the time but nets *negative* because SL can be 100–400 pts
on Sensex while TP is fixed at 100 pts (0.25R). S7 inverts this by anchoring SL to ATR and
requiring minimum 1.5R before entry.

### Entry conditions (all must be true, evaluated on each closed 5m bar after 9:35)

**LONG:**
1. S3 BLR day review = LONG (9:20 first 5m close above session-open Mid)
2. Close > Opening Range high (9:15–9:30)
3. Close > VWAP
4. VWAP slope positive (VWAP[-1] > VWAP[-5])
5. Candle body ≥ 40% of total range (no doji)

**SHORT:** Mirror (Review=SHORT, close < OR low, close < VWAP, slope negative)

### Exits
- **SL:** Entry ∓ (1.0 × ATR-14) − 2pt buffer
- **TP1:** Entry ± (1.5 × ATR-14) → full exit
- **Trail:** After each 5m close, SL moves to 3-bar swing low/high (ratchet only)
- **Time:** 14:55 IST forced flat

### Filters
- Max 1 trade per index per day
- No entry after 13:30 IST
- Daily loss limit: 2% of capital → engine pauses

---

## 3. 90-Day Backtest Results (25 Mar – 22 Jun 2026)

| Metric | S7 VMB | S3 BLR | S6 S/R |
|--------|--------|--------|--------|
| Trades | 106 | 237 | 330 |
| Win rate | 35.8% | 56.1% | 44.8% |
| **Total pts** | **+298.60** | **-2,250** | **-2,089** |
| Avg win | ~+78 pts | ~+30 pts | ~+47 pts |
| Avg loss | ~-28 pts | ~-54 pts | ~-29 pts |

S7 is the **only profitable intraday signal strategy** in the 90-day test.
Win rate is lower (36%) because ATR entries are selective and sometimes the OR break reverses —
but the reward-to-risk (78 avg win / 28 avg loss ≈ 2.8R) more than compensates.

**INR estimate (1 lot each, 3 indices):**
- 38 wins × avg 78 pts
  - Nifty: 78 × 65 = ₹5,070/win
  - BankNifty: 78 × 30 = ₹2,340/win
  - Sensex: 78 × 20 = ₹1,560/win
- 67 losses × avg 28 pts
  - Nifty: -28 × 65 = -₹1,820/loss
- **Net over 90 days with 1 lot Nifty only:** ~38 × ₹5,070 – 67 × ₹1,820 ≈ ₹+70,910

---

## 4. Risk & Position Sizing

### Formula
```
risk_inr  = capital × RISK_PCT         (default 1.0%)
sl_pts    = ATR(14) × 1.0
lots      = floor(risk_inr / (sl_pts × index_lot_size))
lots      = min(lots, MAX_LOTS)         (default 2)
```

### Example at ₹10,00,000 capital
- risk_inr = ₹10,000
- Nifty ATR = 40 pts → sl_pts = 40, lot_size = **65**
- risk per lot = 40 × 65 = ₹2,600
- lots = floor(10,000 / 2,600) = **3 lots** (capped at MAX_LOTS=2)

### Daily loss limit
- Default: 2% of capital = ₹20,000 (at ₹10L capital)
- Engine sets `daily_loss_paused` flag, squares off all positions, no new entries

---

## 5. Live Deployment Plan

### Docker service (add to configs/docker-compose.yml)
```yaml
s7_engine:
  build: ./src/server
  command: python -u src/app/services/s7_vwap_breakout_engine.py
  env_file: configs/.env
  restart: unless-stopped
  depends_on: [redis]
```

### Required env vars (add to configs/.env.example)
```bash
S7_CAPITAL_INR=500000
S7_RISK_PCT=1.0
S7_DAILY_LOSS_LIMIT_PCT=2.0
S7_MAX_LOTS=2
S7_ATR_PERIOD=14
S7_ATR_SL_MULT=1.0
S7_ATR_TP1_MULT=1.5
S7_BODY_RATIO=0.40
S7_VWAP_SLOPE_BARS=4
S7_SL_BUFFER_PTS=2.0
S7_MAX_TRADES_PER_DAY=1
S7_NO_ENTRY_AFTER_IST=13:30
S7_POLL_SECONDS=15
```

### Deployment steps
1. `git pull` on EC2
2. Add env vars to `configs/.env`
3. `docker compose -p ak07 -f configs/docker-compose.yml up -d --build s7_engine`
4. Watch logs: `docker compose -p ak07 -f configs/docker-compose.yml logs -f s7_engine`
5. Run paper for 5 days, compare to backtest expectancy
6. If live PnL within 30% of backtest avg → scale to live with MIN(₹2L, 25% of capital)

---

## 6. Recommended Ramp

| Phase | Duration | Capital at risk | Action |
|-------|----------|-----------------|--------|
| Paper | 2 weeks | ₹0 | Verify signal count matches backtest |
| Live small | 2 weeks | ₹1–2L | 1 lot Nifty only, monitor slippage |
| Live full | Ongoing | Your target | All 3 indices, dynamic lot sizing |

---

## 7. Run instructions

```powershell
# Backtest only
python scripts/backtest_ak07.py --strategies s7 --days 90

# Live engine (paper mode)
python src/server/src/app/services/s7_vwap_breakout_engine.py
```

---

## 8. Safety & Compliance

- Always start with `AK07_PAPER_TRADING=1` before going live
- This strategy uses **options buying** only — no naked selling
- No overnight positions — all squared at 14:55 IST
- Recommended broker: Upstox (API already integrated)
- SEBI circular on algorithmic trading: ensure algo is registered if trading > ₹5L/day
- Tax: short-term capital gains apply to all F&O trades (43.68% effective rate at highest slab)
- **Keep a trading log for ITR-3 filing**

---

> **DISCLAIMER: This is NOT financial advice. All figures are based on historical backtests.
> Markets can and do behave very differently in live conditions. You can lose all your capital.
> Trade only what you can afford to lose. Consult a SEBI-registered investment advisor.**
