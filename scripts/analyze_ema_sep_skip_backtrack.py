import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from zoneinfo import ZoneInfo

# Local imports from the existing codebase.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading_bot import TRADING_CONFIG, UpstoxClient


ORDERS_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - "
    r"(?P<script>[A-Z0-9]+) \| ACTION=SKIP \| SIDE=(?P<side>BUY|SELL) \| PRICE=(?P<price>\d+\.\d{2}) "
    r"\| REASON=(?P<reason>[^|]+) \| (?P<rest>.+)$"
)


ADX_RE = re.compile(r"adx=(?P<adx>\d+\.\d+)")


def _parse_log_dt(s: str) -> datetime:
    # Example: "2026-04-02 13:21:27,485"
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f")


def _floor_to_minutes(dt: datetime, minutes: int) -> datetime:
    # Floor to the start of the minute-bucket.
    discard = timedelta(minutes=dt.minute % minutes, seconds=dt.second, microseconds=dt.microsecond)
    return dt - discard


def _get_script_min_adx(script_name: str) -> float:
    return float(TRADING_CONFIG.get("adx_min_threshold_by_script", {}).get(script_name, TRADING_CONFIG.get("adx_min_threshold", 20.0)))


def _get_order_quantity(script_name: str) -> int:
    overrides = TRADING_CONFIG.get("order_quantity_override_by_script", {}) or {}
    if script_name in overrides:
        try:
            return max(1, int(float(overrides.get(script_name))))
        except Exception:
            pass
    lots = int(TRADING_CONFIG.get("quantity", 1))
    lot_size = int(TRADING_CONFIG.get("lot_sizes", {}).get(script_name, 1))
    return max(1, lots * lot_size)


def _resample_5min(df: pd.DataFrame) -> pd.DataFrame:
    # df columns: timestamp, open, high, low, close, volume, oi
    if df is None or df.empty:
        return df
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    resampled = (
        df.set_index("timestamp")
        .resample("5min")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
                "oi": "last",
            }
        )
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )
    return resampled


def _get_entry_swing_sl(df5: pd.DataFrame, entry_candle_timestamp: datetime, side: str) -> float | None:
    # Mirrors TradingBot._get_entry_swing_sl
    if df5 is None or df5.empty or entry_candle_timestamp is None:
        return None

    if "timestamp" not in df5.columns or "open" not in df5.columns:
        return None

    working = df5.sort_values("timestamp").reset_index(drop=True)
    eligible = working[working["timestamp"] <= pd.to_datetime(entry_candle_timestamp)]
    if eligible.empty:
        return None

    entry_idx = int(eligible.index[-1])
    prev_idx = entry_idx - 1
    if prev_idx < 0:
        return None

    lookback = max(1, int(TRADING_CONFIG.get("ema_long", 18)))
    start_idx = max(0, prev_idx - lookback + 1)

    for idx in range(prev_idx, start_idx - 1, -1):
        row = working.iloc[idx]
        is_bearish = float(row["close"]) < float(row["open"])
        is_bullish = float(row["close"]) > float(row["open"])

        if side == "BUY" and is_bearish:
            return float(row["low"])
        if side == "SELL" and is_bullish:
            return float(row["high"])
    return None


def _profit_lock_ladder_for_script(script_name: str):
    raw_ladder = TRADING_CONFIG.get("profit_lock_ladder_by_script", {}).get(script_name, TRADING_CONFIG.get("profit_lock_ladder", []))
    ladder = []
    for rule in raw_ladder:
        if not isinstance(rule, dict):
            continue
        try:
            trigger_r = float(rule.get("trigger_r", 0))
            lock_r = float(rule.get("lock_r", 0))
        except (TypeError, ValueError):
            continue
        if trigger_r <= 0 or lock_r <= 0:
            continue
        lock_r = min(lock_r, trigger_r)
        ladder.append((trigger_r, lock_r))
    ladder.sort(key=lambda x: x[0])
    return ladder


def _trailing_rule_for_script(script_name: str, risk_percent: float) -> tuple[float, float]:
    overrides = TRADING_CONFIG.get("trailing_overrides_by_script", {}) or {}
    script_rule = overrides.get(script_name, {}) or {}
    breakeven_trigger_percent = float(script_rule.get("breakeven_trigger_percent", risk_percent))
    trail_step_percent = float(script_rule.get("trail_step_percent", TRADING_CONFIG.get("trail_step_percent", 0.5)))
    return breakeven_trigger_percent, trail_step_percent


def _apply_profit_lock_ladder(script_name: str, position: dict, favorable_move: float, risk_percent: float, trigger_basis_percent: float) -> bool:
    if risk_percent <= 0:
        return False

    ladder = _profit_lock_ladder_for_script(script_name)
    if not ladder:
        return False

    entry_price = float(position["entry_price"])
    position_type = str(position["type"]).upper()
    initial_sl = float(position.get("initial_sl", position.get("stop_loss", entry_price)))
    risk_points = abs(entry_price - initial_sl)
    if risk_points <= 0:
        return False

    basis_percent = float(trigger_basis_percent if trigger_basis_percent and trigger_basis_percent > 0 else risk_percent)
    current_r = favorable_move / basis_percent

    best_rule = None
    for trigger_r, lock_r in ladder:
        if current_r >= trigger_r:
            best_rule = (trigger_r, lock_r)
        else:
            break
    if best_rule is None:
        return False

    trigger_r, lock_r = best_rule
    locked_r = float(position.get("profit_lock_r_locked", 0.0) or 0.0)
    if lock_r <= locked_r + 1e-9:
        return False

    if position_type == "BUY":
        lock_sl = entry_price + (lock_r * risk_points)
        new_sl = max(float(position["stop_loss"]), lock_sl)
    else:
        lock_sl = entry_price - (lock_r * risk_points)
        new_sl = min(float(position["stop_loss"]), lock_sl)

    if abs(new_sl - float(position["stop_loss"])) < 1e-9:
        return False

    position["stop_loss"] = new_sl
    position["profit_lock_r_locked"] = lock_r
    position["profit_lock_trigger_r_locked"] = trigger_r
    return True


def _calculate_realized_pnl(position_type: str, entry_price: float, exit_price: float, quantity: float) -> float:
    if position_type == "BUY":
        return (exit_price - entry_price) * quantity
    return (entry_price - exit_price) * quantity


def _favorable_move_percent(position_type: str, entry_price: float, current_price: float) -> float:
    if position_type == "BUY":
        return ((current_price - entry_price) / entry_price) * 100.0
    return ((entry_price - current_price) / entry_price) * 100.0


def _calculate_stepped_sl_with_percent(position_type: str, entry_price: float, steps: int, step_percent: float) -> float:
    step_fraction = step_percent / 100.0
    if position_type == "BUY":
        return entry_price * (1 + step_fraction * steps)
    return entry_price * (1 - step_fraction * steps)


def _update_position_sl(script_name: str, position: dict, current_price: float) -> bool:
    entry_price = float(position["entry_price"])
    position_type = str(position["type"]).upper()

    initial_sl = float(position.get("initial_sl", position.get("stop_loss", entry_price)))
    if entry_price > 0:
        risk_percent = abs((entry_price - initial_sl) / entry_price) * 100.0
    else:
        risk_percent = 0.0
    if risk_percent <= 0:
        risk_percent = float(TRADING_CONFIG.get("trailing_stop_loss_percent", 1.0))

    breakeven_trigger_percent, step_percent = _trailing_rule_for_script(script_name, risk_percent)
    effective_breakeven_trigger_percent = min(float(breakeven_trigger_percent), float(risk_percent))

    favorable_move = _favorable_move_percent(position_type, entry_price, float(current_price))
    quantity = float(position.get("quantity", _get_order_quantity(script_name)))
    favorable_pnl = _calculate_realized_pnl(position_type, entry_price, float(current_price), quantity)

    sl_updated = False
    max_favorable = float(position.get("max_favorable_pnl", 0.0) or 0.0)
    if favorable_pnl > max_favorable:
        position["max_favorable_pnl"] = favorable_pnl

    if favorable_move < effective_breakeven_trigger_percent and not bool(position.get("breakeven_done", False)):
        return sl_updated

    if not bool(position.get("breakeven_done", False)):
        # 1:1 reached: SL to cost
        if position_type == "BUY":
            position["stop_loss"] = max(float(position["stop_loss"]), entry_price)
        else:
            position["stop_loss"] = min(float(position["stop_loss"]), entry_price)
        position["breakeven_done"] = True
        sl_updated = True

    # Profit-lock ladder
    locked = _apply_profit_lock_ladder(
        script_name=script_name,
        position=position,
        favorable_move=favorable_move,
        risk_percent=risk_percent,
        trigger_basis_percent=effective_breakeven_trigger_percent,
    )
    if locked:
        sl_updated = True

    # Step trail
    extra_move = max(0.0, favorable_move - effective_breakeven_trigger_percent)
    new_steps = int(extra_move // float(step_percent))
    if new_steps > int(position.get("trail_steps_locked", 0) or 0):
        position["trail_steps_locked"] = new_steps
        stepped_sl = _calculate_stepped_sl_with_percent(position_type, entry_price, new_steps, step_percent)
        if position_type == "BUY":
            position["stop_loss"] = max(float(position["stop_loss"]), stepped_sl)
        else:
            position["stop_loss"] = min(float(position["stop_loss"]), stepped_sl)
        sl_updated = True
    return sl_updated


def simulate_one_trade(
    script_name: str,
    side: str,
    entry_time: datetime,
    entry_price: float,
    df5: pd.DataFrame,
    horizon_candles: int = 72,
):
    IST = ZoneInfo("Asia/Kolkata")

    # side is BUY/SELL (bot "type")
    # Determine candle buckets based on bot logic:
    # - entry_candle_timestamp = last closed 5m bucket start = floor_to_5(entry_time) - 5min
    # - current bucket start = floor_to_5(entry_time)
    working = df5.sort_values("timestamp").reset_index(drop=True)
    ts_col = pd.to_datetime(working["timestamp"])
    if pd.api.types.is_datetime64tz_dtype(ts_col.dtype):
        # Bot logs are in IST; ensure comparisons are tz-aware.
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=IST)

    floored = _floor_to_minutes(entry_time, 5)
    entry_candle_ts = floored - timedelta(minutes=5)
    current_bucket_ts = floored

    # Locate closest indices <= bucket timestamps (df timestamp should align to bucket starts)
    eligible_entry = working[ts_col <= pd.to_datetime(entry_candle_ts)]
    eligible_current = working[ts_col <= pd.to_datetime(current_bucket_ts)]
    if eligible_entry.empty or eligible_current.empty:
        return {"outcome": "NO_DATA"}

    entry_idx = int(eligible_entry.index[-1])
    current_idx = int(eligible_current.index[-1])
    if entry_idx <= 0:
        return {"outcome": "SL_INVALID", "reason": "no prev candle for swing SL"}

    initial_sl = _get_entry_swing_sl(working, entry_candle_ts, side)
    if initial_sl is None:
        return {"outcome": "SL_INVALID", "reason": "swing SL is None"}

    # Mirror entry validity check
    if side == "BUY" and initial_sl >= entry_price:
        return {"outcome": "SL_INVALID", "reason": "SL >= entry (BUY) "}
    if side == "SELL" and initial_sl <= entry_price:
        return {"outcome": "SL_INVALID", "reason": "SL <= entry (SELL) "}

    target_percent = float(TRADING_CONFIG.get("target_percent", 2.0))
    target_price = float(entry_price) * (1 + target_percent / 100.0) if side == "BUY" else float(entry_price) * (1 - target_percent / 100.0)
    qty = _get_order_quantity(script_name)

    position = {
        "type": side,
        "entry_price": float(entry_price),
        "initial_sl": float(initial_sl),
        "stop_loss": float(initial_sl),
        "target_price": float(target_price),
        "quantity": float(qty),
        "trail_steps_locked": 0,
        "breakeven_done": False,
        "profit_lock_r_locked": 0.0,
        "profit_lock_trigger_r_locked": 0.0,
        "max_favorable_pnl": 0.0,
        "money_lock_steps_locked": 0,
        "money_lock_pnl_locked": 0.0,
        "last_polled_price": float(entry_price),
    }

    # Start from the candle AFTER the "current bucket" close
    start_idx = current_idx + 1
    end_idx = min(len(working) - 1, start_idx + horizon_candles - 1)

    prev_polled_price = float(entry_price)
    for i in range(start_idx, end_idx + 1):
        row = working.iloc[i]
        current_price = float(row["close"])

        # Update trailing SL first (matches bot order)
        _update_position_sl(script_name, position, current_price)

        stop_loss = float(position["stop_loss"])

        # Stop-loss check (matches BUY/SELL logic)
        if side == "BUY":
            sl_hit = (current_price <= stop_loss) or (prev_polled_price > stop_loss and current_price <= stop_loss)
        else:
            sl_hit = (current_price >= stop_loss) or (prev_polled_price < stop_loss and current_price >= stop_loss)

        if sl_hit:
            trailing = abs(float(position["stop_loss"]) - float(position["initial_sl"])) > 1e-9
            outcome = "TRAILING_STOP_LOSS_HIT" if trailing else "STOP_LOSS_HIT"
            pnl = _calculate_realized_pnl(side, float(entry_price), current_price, qty)
            return {"outcome": outcome, "exit_price": current_price, "pnl": pnl}

        # Target check
        if side == "BUY":
            target_hit = (current_price >= target_price) or (prev_polled_price < target_price and current_price >= target_price)
        else:
            target_hit = (current_price <= target_price) or (prev_polled_price > target_price and current_price <= target_price)

        if target_hit:
            pnl = _calculate_realized_pnl(side, float(entry_price), current_price, qty)
            return {"outcome": "TARGET_HIT", "exit_price": current_price, "pnl": pnl}

        prev_polled_price = current_price
        position["last_polled_price"] = current_price

    return {"outcome": "NONE", "last_price": float(working.iloc[end_idx]["close"]) if end_idx >= start_idx else float(entry_price)}


def main():
    repo_root = Path(__file__).resolve().parents[1]
    user = "AK07"
    orders_log = repo_root / "server" / "data" / "users" / user / "logs" / "orders.log"
    creds_path = repo_root / "server" / "data" / "users" / user / "upstox_credentials.json"

    if not orders_log.exists():
        raise SystemExit(f"orders.log not found: {orders_log}")
    if not creds_path.exists():
        raise SystemExit(f"credentials not found: {creds_path}")

    creds = json.loads(creds_path.read_text(encoding="utf-8"))
    access_token = (creds.get("access_token") or "").strip()
    base_url = (creds.get("base_url") or "https://api.upstox.com/v2").strip()

    # Parse skip events (EMA_SEPARATION_TOO_SMALL)
    events = []
    for line in orders_log.read_text(encoding="utf-8").splitlines():
        m = ORDERS_LOG_LINE_RE.match(line.strip())
        if not m:
            continue
        ts = _parse_log_dt(m.group("ts"))
        script = m.group("script")
        side = m.group("side")
        reason = m.group("reason").strip()
        if reason != "EMA_SEPARATION_TOO_SMALL":
            continue
        entry_price = float(m.group("price"))
        rest = m.group("rest")
        adx_m = ADX_RE.search(rest)
        adx_val = float(adx_m.group("adx")) if adx_m else None
        if adx_val is None:
            continue
        min_adx = _get_script_min_adx(script)
        if adx_val < min_adx:
            continue  # only analyze cases where ADX is in "our range"
        events.append(
            {
                "script": script,
                "side": side,
                "time": ts,
                "entry_price": entry_price,
                "adx": adx_val,
                "min_adx": min_adx,
            }
        )

    if not events:
        print("No EMA_SEPARATION_TOO_SMALL SKIP events with ADX >= min_adx found in orders.log.")
        return

    # Group per script to minimize candle fetches
    scripts = sorted({e["script"] for e in events})
    client = UpstoxClient(access_token, base_url, username=user, log=None)

    # Fetch candles around the events (use a small window)
    min_dt = min(e["time"] for e in events)
    max_dt = max(e["time"] for e in events)
    from_date = (min_dt - timedelta(days=2)).strftime("%Y-%m-%d")
    to_date = max_dt.strftime("%Y-%m-%d")
    interval_1m = TRADING_CONFIG.get("interval", "1minute")

    df5_by_script = {}
    for script_name in scripts:
        instrument_key = TRADING_CONFIG["scripts"][script_name]
        df_hist = client.get_historical_candles(instrument_key, interval_1m, from_date, to_date)
        df_intraday = client.get_intraday_candles(instrument_key, interval_1m)
        if df_hist is None and df_intraday is None:
            raise SystemExit(f"No candles returned for {script_name} instrument={instrument_key}")
        if df_hist is not None and df_intraday is not None:
            df = pd.concat([df_hist, df_intraday], ignore_index=True)
            df = df.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp").reset_index(drop=True)
        else:
            df = df_hist if df_hist is not None else df_intraday
        df5 = _resample_5min(df)
        df5_by_script[script_name] = df5

    print(f"Backtest-style simulation for {user} using orders.log skip events.")
    print(f"Window: {from_date} to {to_date}, scripts={scripts}")
    print("")

    total = {"TARGET_HIT": 0, "STOP_LOSS_HIT": 0, "TRAILING_STOP_LOSS_HIT": 0, "NONE": 0, "SL_INVALID": 0, "NO_DATA": 0}
    rows = []

    for idx, ev in enumerate(sorted(events, key=lambda x: x["time"])):
        script = ev["script"]
        df5 = df5_by_script[script]
        res = simulate_one_trade(
            script_name=script,
            side=ev["side"],
            entry_time=ev["time"],
            entry_price=ev["entry_price"],
            df5=df5,
            horizon_candles=72,  # 72 * 5min ~ 6 hours
        )
        outcome = res.get("outcome")
        total[outcome] = total.get(outcome, 0) + 1
        pnl = res.get("pnl")
        rows.append(
            {
                "event": idx + 1,
                "script": script,
                "side": ev["side"],
                "time": ev["time"].strftime("%Y-%m-%d %H:%M:%S"),
                "adx": ev["adx"],
                "min_adx": ev["min_adx"],
                "entry_price": ev["entry_price"],
                "initial_note": res.get("reason") or "",
                "outcome": outcome,
                "exit_price": res.get("exit_price"),
                "pnl": pnl,
            }
        )

    print("Results (hypothetical if we allowed EMA_SEPARATION_TOO_SMALL entries when ADX >= min_adx):")
    for r in rows:
        note = f" ({r['initial_note']})" if r.get("initial_note") else ""
        pnl_txt = f", pnl={r['pnl']:.2f}" if r.get("pnl") is not None else ""
        exit_txt = f", exit={r['exit_price']:.2f}" if r.get("exit_price") is not None else ""
        print(
            f"- #{r['event']} {r['script']} {r['side']} @ {r['time']} entry={r['entry_price']:.2f} "
            f"ADX={r['adx']:.2f} (min_adx={r['min_adx']:.2f}) -> {r['outcome']}{exit_txt}{pnl_txt}{note}"
        )

    print("")
    print("Summary:")
    for k in ["TARGET_HIT", "STOP_LOSS_HIT", "TRAILING_STOP_LOSS_HIT", "NONE", "SL_INVALID", "NO_DATA"]:
        if k in total:
            print(f"- {k}: {total[k]}")


if __name__ == "__main__":
    main()

