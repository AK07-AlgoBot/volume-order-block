# Orderflow Strategy Bench (7 strategies)

One place to test the 7 orderflow strategies pulled together on 2026‑09‑15.
Each strategy is testable by **one of two routes**:

- **Lipi** — load the `.lipi` file on a GoCharting 5m footprint chart (NIFTY‑I / BANKNIFTY‑I). Signals confirm on bar close; alerts emit `OFS|Sx|BUY|<sym>|5`.
- **Upstox** — run the existing Python engine (already wired to Upstox candles / backtest).

> Lipi note (hard‑won): no `var`, no nested `for` loops (times out), don't init a
> reassigned variable with `na` (use `0.0`/`-1.0`), no `plot.style_*` args.
> Historical orderflow indexing `orderflow.delta[i]` **is** supported.

## Index

| # | Strategy | Core idea | Route | File / entry point | Status |
|---|----------|-----------|-------|--------------------|--------|
| **S1** | Reversal (swing Δ‑divergence + BOS) | Lower low but higher delta (absorption) → turn; BOS confirms trend | Lipi | `../scripts/gocharting/OFS_Reversal_v1.lipi` | ✅ built |
| **S2** | Exhaustion | New extreme while volume **and** \|delta\| fade → reversal | Lipi | `gocharting/OFS_S2_Exhaustion.lipi` | ✅ built |
| **S3** | Cumulative‑delta divergence ⭐ | Fresh low + **CVD** higher low + Δ‑flip + pressure. **Promoted to reference.** | Lipi | `../scripts/orderflow/reference/OFS_S3_CVD_Divergence.lipi` | ✅ kept |
| **S6** | SR reversal (1H structural anchors) | Anchor 09:15 to last confirmed 1H swing H/L; fade/reclaim | Upstox | `../src/server/src/app/services/sr_reversal_engine.py` | ✅ exists |
| **S7** | CHoCH 5m reversal (HTF + ADX) | Close breaks structure vs trend; 1H EMA20 + ADX>20 filter | Upstox | `../src/server/src/app/services/choch_engine.py` | ✅ backtested |

**Dropped:** S4 (POC + delta‑flip) and S5 (Initiative) — removed 2026‑09‑15, too many/confusing signals.

## How to test — Lipi (S1–S3)

1. GoCharting → 5m chart of `NSE:NIFTY-I` (or `BANKNIFTY-I`) with **Footprint / Orderflow** enabled (delta available).
2. Study → New Lipi → paste the `.lipi` file → Apply.
3. Read the BUY/SELL triangles (confirm on the closed bar). Set alerts on the `OFS|Sx|...` conditions to pipe into the executor.
4. Same script auto‑scales NIFTY vs BANKNIFTY (`isBN = close > 40000`).

## How to test — Upstox (S6, S7)

```bash
# CHoCH (Strategy 8 in the tree) — 90‑day validated, Sharpe 5.16
python -u src/server/src/app/services/choch_engine.py

# SR reversal (Strategy 6) — 1H structural anchors
python -u src/server/src/app/services/sr_reversal_engine.py
```
Backtest artifacts already in repo: `choch_compare_3y.json`, `backtest_choch*.py`.

## Reference logic (Python, not runnable as‑is)

The "Fabio‑style" detectors these strategies are distilled from live in
`../scripts/orderflow/reference/` (imports point at `orderflow_system.*`):
`patterns/{divergence,absorption,exhaustion,initiative,sweep}.py`,
`analytics/{delta,footprint,volume_profile,orderbook}.py`.
Port into Lipi (bar‑level) or into an Upstox engine (needs live footprint via
`scripts/orderflow/upstox_ofmap_bridge.py`).

## Not portable to Lipi (need true footprint / L2)

- Per‑price bid/ask **imbalance stacking** and **book sweep** (L2 consumption) —
  require the live OrderFlowMap feed, not GoCharting bar aggregates.
