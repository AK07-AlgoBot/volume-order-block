# Options companion trading logic

This document describes how the trading bot (`src/bot/trading_bot.py`) handles **index options** opened alongside **index futures** entries, and how that differs from pure futures behaviour.

## Upstox index F&O — lot size (reference)

| Symbol | Quantity per 1 lot | Notes |
|--------|-------------------|--------|
| **NIFTY** | **65** | `lot_sizes["NIFTY"]` |
| **BANKNIFTY** | **30** | `lot_sizes["BANKNIFTY"]` |
| **SENSEX** | **20** | `lot_sizes["SENSEX"]` |

With default **`quantity`: 1** (one futures lot), `_get_order_quantity` is **65 / 30 / 20** respectively. The options companion uses the same lot sizes from the instrument master for option legs (`options_total_lots` × contract lot size).

## Expiry selection

| Underlying   | Policy |
|-------------|--------|
| **NIFTY**, **SENSEX** | **Nearest** FO expiry in the chain (in practice the front **weekly** series). |
| **BANKNIFTY**       | **Nearest monthly** expiry: contracts whose IST calendar expiry is the **last Thursday** of the month. If the master has no such row, the bot falls back to the nearest expiry and logs a warning. |

## Expiry day — no new options after noon (IST)

- Config: `options_expiry_day_cutoff_ist` (default `"12:00"`).
- If the **chosen option’s expiry date** (IST) is **today** and the current time is **on or after** that cutoff, `_start_options_companion` **does not** open a new option leg. A clear `OPTIONS SKIP` line is written to the bot log.

## Expiry day — index futures contract

For **NIFTY**, **BANKNIFTY**, and **SENSEX** futures:

- On the **front** contract’s **expiry day** (IST), new seeding and selection prefer the **next serial** futures month instead of the contract that expires that session.
- While the bot is running, `_maybe_refresh_index_futures_tokens_expiry_day` (throttled, every ~120s) advances `scripts` / `order_tokens` if they still point at the expiring front when the exchange date is expiry day.

This avoids taking new futures exposure on the illiquid / settling front on roll day.

## Exit modes (`options_exit_mode`)

| Value | Behaviour |
|-------|-----------|
| **`ladder_gtt`** (default) | After the option **market** order, resolve **average fill** (`order/trades` when possible, else LTP). Compute premium **R** = \|BS delta\| × \|futures entry − futures SL\| (see `options_use_bs_delta_for_r`, `options_iv_annual`). Place **four** GTTs on the **option**: one **SL** (full qty, `entry − R`), three **TP**s with quantities from `options_tp_lot_splits` (default **2+1+1** lots) at `entry+R`, `entry+2R`, `entry+3R`. Trailing futures SL triggers **cancel + replace** on the **SL** GTT only. Broker net quantity is polled to refresh SL at **breakeven** after TP legs fill. Optional **EMA crossover** on **option** candles exits the rest (`OPT_CHART_CROSSOVER`). |
| **`legacy_underlying`** | Older flow: single SL GTT + partial exits when **underlying** hits R-levels. |

## Stop-loss on options (recommendation and implementation)

**Recommendation:** Relying only on live **Greeks** from the broker is fragile (latency, availability, IV jumps). Using only the **underlying** ignores convexity and premium crush. The bot uses a **hybrid**:

1. **Primary:** Map the **futures-defined risk** (`entry_underlying` → `sl_underlying`) into a **protective sell** trigger on the long option using an **assumed ATM delta** (`options_sl_delta_assumption`, default `0.5`):  
   `trigger ≈ entry_option − delta × |entry_underlying − sl_underlying|`.
2. **Floor:** Never place the trigger below **`options_sl_min_premium_floor_ratio`** × entry premium (default `0.35`), so the stop is not absurdly deep in a gap or bad tick.
3. **Trailing sync:** Each management loop can **ratchet** the GTT trigger **up** when the futures trailing SL tightens (`_sync_option_sl_from_underlying_model`), with a small price step threshold to limit API churn.
4. **R-multiple stages** (1R / 2R / final) still move underlying milestones and may reset option stops from **live option LTP** at those events; the trigger is never reduced below the previous value at 1R.

With **`options_use_bs_delta_for_r`:** true, **ladder** mode uses **Black–Scholes delta** (`src/lib/option_greeks.py`) with **`options_iv_annual`** and time-to-expiry from the contract for **R** in **premium points** (e.g. ~83 when futures risk is 166 pts and delta ≈ 0.5). If BS is disabled, the same **fixed delta** as below is used.

Greeks are **not** required for **legacy** mode; you can lower `options_sl_delta_assumption` toward live ATM delta for finer mapping.

## Profit booking on options vs ₹ futures target

**Ladder GTT mode:** take-profit is handled by **exchange GTTs** at 1:1 / 1:2 / 1:3 in **premium** space; the rupee overlay below applies only to **`legacy_underlying`**.

Futures use `nse_trade_pnl_levels.target_pnl` (e.g. ₹5000) in **price** space via `_nse_rupee_sl_target_prices`. Options use a separate **rupee P&L** overlay:

- Config block: `options_rupee_profit_booking` (`enabled`, `lot_fraction`, optional `futures_lots_reference`).
- **Scaled target:**  
  `target_rupees = nse_trade_pnl_levels.target_pnl × (options_total_lots / futures_lots_reference)`  
  with `futures_lots_reference` defaulting to `quantity` (futures lots in config). Example: ₹5000 target and the same `quantity` as the companion futures sizing → same rupee target; if you run **more** option lots than futures lots, the target scales **up** proportionally.
- When **mark-to-market** premium P&amp;L on the **remaining** quantity reaches that target, the bot books **`lot_fraction`** of remaining lots (ceil, at least 1), reason `OPT_RUPEE_TP`, sets `options_rupee_tp_done`, refreshes the GTT, and logs `OPTIONS RUPEE TP`.

This is **additive** to the existing **R-based** partial exits (`OPT_TP_1R`, etc.); tune fractions if you want less overlap.

## Logging

Look for these prefixes in `trading_bot.log` / console:

- `OPTIONS CHAIN:` — expiry policy applied.
- `OPTIONS SKIP:` — expiry-day noon cutoff.
- `OPTIONS ENTRY:` / `orders.log` companion lines — strike, hybrid SL, expiry.
- `OPTIONS SL SYNC:` — underlying-driven ratchet on the option GTT.
- `OPTIONS RUPEE TP:` — scaled rupee profit hit.
- `INDEX FUTURES:` / `INDEX FUTURES ROLL:` — expiry-day next-month futures selection or token refresh.

## Related config keys (TRADING_CONFIG)

| Key | Role |
|-----|------|
| `options_enabled`, `options_scripts`, `options_total_lots`, `options_target3_r` | Companion on/off, symbols, size, final R multiple. |
| `options_expiry_day_cutoff_ist` | IST cutoff on option expiry day. |
| `options_sl_delta_assumption`, `options_sl_min_premium_floor_ratio` | Hybrid SL mapping. |
| `options_rupee_profit_booking` | Scaled rupee take-profit on the option leg. |
| `options_gtt_enabled` | Enable/disable GTT placement for option SL. |
| `options_exit_mode` | `ladder_gtt` or `legacy_underlying`. |
| `options_tp_lot_splits` | Three integers summing to `options_total_lots` (e.g. `[2,1,1]`). |
| `options_use_bs_delta_for_r`, `options_iv_annual`, `options_risk_free_rate` | Ladder premium **R** from BS delta. |
| `options_chart_crossover_exit`, `options_crossover_interval` | EMA crossover on **option** chart for emergency exit. |
