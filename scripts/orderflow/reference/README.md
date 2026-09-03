# Orderflow reference extracts (kept from repo review)

**Kept live tool:** `C:\Users\pavan\arun\OrderFlowMap` → http://localhost:7890

This folder holds the only pieces worth keeping from the other two repos we tested and then removed.

## Kept from OrderFlow-Analysis-Pro

Pattern detectors (Fabio-style) — useful for a future Upstox/Nifty signal engine:

| File | Why keep |
|------|----------|
| `patterns/absorption.py` | High effort / low result → absorption entry |
| `patterns/initiative.py` | Strong delta + follow-through → trail / join |
| `patterns/sweep.py` | Thin book vacuum / sweep |
| `patterns/exhaustion.py` | Declining volume at extremes → exit / reverse |
| `patterns/divergence.py` | Price vs CVD divergence (same idea as AK07 DD/FD) |
| `analytics/delta.py` | Vertical / horizontal / cumulative delta |
| `analytics/footprint.py` | Bid/ask per price level + imbalance |
| `analytics/volume_profile.py` | POC / VAH / VAL |
| `analytics/orderbook.py` | L2 thin-level / consumption tracking |
| `data/models.py` | Tick, Candle, Signal, FootprintLevel types |

These are **reference only** — imports still point at `orderflow_system.*`. Port logic into AK07 before using live.

## Kept from OrderflowChart (archived)

| File | Why keep |
|------|----------|
| `orderflowchart_imbalance_snippet.py` | Plotly footprint imbalance calc from bid/ask sizes |

Rest of that repo was static CSV plotting only — not kept.

## Not kept

- Bybit/MT5 feeds, FastAPI dashboard, Telegram bot (not NSE / not needed)
- Full OrderflowChart Plotly renderer (broken on pandas 2.x, no live feed)
