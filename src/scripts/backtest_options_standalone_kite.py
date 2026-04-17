from __future__ import annotations

import argparse
import csv
import io
import math
import sys
import time as pytime
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (REPO_ROOT, REPO_ROOT / "src" / "lib", REPO_ROOT / "src" / "bot"):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from kite_fut_instrument import resolve_kite_instrument_token
from kite_rest_candles import fetch_historical_raw, kite_candles_to_dataframe, map_bot_interval_to_kite
from option_greeks import bs_call_delta, bs_put_delta, years_to_expiry_from_ms
from trading_bot import TRADING_CONFIG
from zerodha_credentials_store import load_zerodha_credentials_for_user

IST = ZoneInfo("Asia/Kolkata")
KITE_ROOT = "https://api.kite.trade"


@dataclass
class TradeResult:
    script: str
    option_symbol: str
    side: str
    entry_ts: datetime
    exit_ts: datetime
    entry_underlying: float
    entry_option: float
    sl_option: float
    tp3_option: float
    exit_option: float
    outcome: str
    reason: str
    strike: float
    expiry: str


def _trade_pnl_points(r: TradeResult) -> float:
    # Backtest models long options for both CE/PE entries.
    return float(r.exit_option) - float(r.entry_option)


def _trade_r_multiple(r: TradeResult) -> float:
    risk = float(r.entry_option) - float(r.sl_option)
    if risk <= 0:
        return 0.0
    return _trade_pnl_points(r) / risk


def _headers(api_key: str, access_token: str) -> dict[str, str]:
    return {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{access_token}",
    }


def _fetch_instruments_csv(api_key: str, access_token: str, exchange: str) -> str:
    import requests

    r = requests.get(
        f"{KITE_ROOT.rstrip('/')}/instruments/{exchange.strip().upper()}",
        headers=_headers(api_key, access_token),
        timeout=180,
    )
    r.raise_for_status()
    return r.text


def _calculate_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _calculate_signals(df: pd.DataFrame, short_period: int, long_period: int) -> pd.DataFrame:
    out = df.copy()
    out["ema_short"] = _calculate_ema(out["close"], short_period)
    out["ema_long"] = _calculate_ema(out["close"], long_period)
    out["signal"] = 0
    out.loc[out["ema_short"] > out["ema_long"], "signal"] = 1
    out.loc[out["ema_short"] < out["ema_long"], "signal"] = -1
    out["prev_signal"] = out["signal"].shift(1)
    out["crossover"] = (out["signal"] != out["prev_signal"]) & (out["prev_signal"] != 0)
    return out


def _calculate_adx_values(df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        pd.Series(up_move).where((up_move > down_move) & (up_move > 0), 0.0), index=df.index
    )
    minus_dm = pd.Series(
        pd.Series(down_move).where((down_move > up_move) & (down_move > 0), 0.0), index=df.index
    )
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    alpha = 1.0 / max(1, int(period))
    atr = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_dm_sm = plus_dm.ewm(alpha=alpha, adjust=False).mean()
    minus_dm_sm = minus_dm.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100.0 * (plus_dm_sm / atr.replace(0, float("nan")))
    minus_di = 100.0 * (minus_dm_sm / atr.replace(0, float("nan")))
    dx = 100.0 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan")))
    adx = dx.ewm(alpha=alpha, adjust=False).mean()
    return adx.fillna(0.0), plus_di.fillna(0.0), minus_di.fillna(0.0)


def _calculate_session_vwap(df: pd.DataFrame) -> pd.Series:
    work = df.reset_index(drop=True)
    day = pd.to_datetime(work["timestamp"], errors="coerce").dt.normalize()
    tp = (work["high"].astype(float) + work["low"].astype(float) + work["close"].astype(float)) / 3.0
    vol = work["volume"].astype(float).clip(lower=0.0)
    pv = tp * vol
    cum_pv = pv.groupby(day).cumsum()
    cum_v = vol.groupby(day).cumsum().replace(0.0, float("nan"))
    return pd.Series((cum_pv / cum_v).to_numpy(), index=df.index)


def _calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    c = close.astype(float)
    delta = c.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    alpha = 1.0 / max(1, int(period))
    avg_gain = gain.ewm(alpha=alpha, adjust=False).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def _coerce_ohlcv_numeric(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in ("open", "high", "low", "close", "volume", "oi"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"])
    if "volume" in out.columns:
        out["volume"] = out["volume"].fillna(0.0)
    return out


def _resample(df: pd.DataFrame, mins: int) -> pd.DataFrame:
    if mins <= 1:
        return df.copy()
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df.columns:
        agg["volume"] = "sum"
    if "oi" in df.columns:
        agg["oi"] = "last"
    return (
        df.set_index("timestamp")
        .sort_index()
        .resample(f"{mins}min")
        .agg(agg)
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )


def _get_entry_swing_sl(df: pd.DataFrame, idx: int, side: str, lookback: int) -> float | None:
    if idx < 0:
        return None
    lo = max(0, idx - lookback + 1)
    w = df.iloc[lo : idx + 1]
    if w.empty:
        return None
    if side == "BUY":
        return float(w["low"].min())
    return float(w["high"].max())


def _is_last_thursday(d: date) -> bool:
    return d.weekday() == 3 and (d + timedelta(days=7)).month != d.month


def _parse_option_master(csv_text: str, script: str) -> list[dict]:
    rows: list[dict] = []
    want_name = script.upper()
    reader = csv.DictReader(io.StringIO(csv_text))
    for r in reader:
        seg = (r.get("segment") or "").strip().upper()
        if seg not in {"NFO-OPT", "BFO-OPT"}:
            continue
        if (r.get("name") or "").strip().upper() != want_name:
            continue
        typ = (r.get("instrument_type") or "").strip().upper()
        if typ not in {"CE", "PE"}:
            continue
        try:
            strike = float(r.get("strike") or 0.0)
            token = int(float(r.get("instrument_token") or 0))
            lot = int(float(r.get("lot_size") or 0))
            exp = datetime.strptime((r.get("expiry") or "")[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if strike <= 0 or token <= 0:
            continue
        rows.append(
            {
                "type": typ,
                "strike": strike,
                "token": token,
                "symbol": (r.get("tradingsymbol") or "").strip().upper(),
                "expiry": exp,
                "lot_size": max(1, lot),
            }
        )
    return rows


def _pick_expiry(script: str, rows: list[dict], as_of: date) -> date | None:
    cands = sorted({r["expiry"] for r in rows if r["expiry"] >= as_of})
    if not cands:
        return None
    if script.upper() != "BANKNIFTY":
        return cands[0]
    monthly = [d for d in cands if _is_last_thursday(d)]
    return monthly[0] if monthly else cands[0]


def _pick_atm_option(
    script: str,
    rows: list[dict],
    as_of: date,
    spot: float,
    opt_type: str,
) -> dict | None:
    exp = _pick_expiry(script, rows, as_of)
    if exp is None:
        return None
    filt = [r for r in rows if r["expiry"] == exp and r["type"] == opt_type]
    if not filt:
        return None
    filt.sort(key=lambda r: (abs(float(r["strike"]) - float(spot)), float(r["strike"])))
    return filt[0]


def _expiry_ms(exp: date) -> int:
    dt = datetime.combine(exp, time(15, 30), tzinfo=IST)
    return int(dt.timestamp() * 1000)


def _premium_r(
    fut_entry: float,
    fut_sl: float,
    strike: float,
    expiry: date,
    opt_type: str,
    iv: float,
    rfr: float,
) -> float:
    risk = abs(float(fut_entry) - float(fut_sl))
    if risk <= 0:
        risk = max(1.0, abs(float(fut_entry)) * 0.002)
    t = years_to_expiry_from_ms(_expiry_ms(expiry))
    if opt_type == "CE":
        d = abs(float(bs_call_delta(float(fut_entry), float(strike), t, iv, rfr)))
    else:
        d = abs(float(bs_put_delta(float(fut_entry), float(strike), t, iv, rfr)))
    return max(1.0, d * risk)


def _simulate_option_trade(
    opt_df: pd.DataFrame,
    entry_idx: int,
    entry_opt: float,
    sl_opt: float,
    tp3_opt: float,
    max_hold_bars: int,
) -> tuple[str, int, float, str]:
    end_idx = min(len(opt_df) - 1, entry_idx + max_hold_bars)
    for i in range(entry_idx + 1, end_idx + 1):
        row = opt_df.iloc[i]
        lo = float(row["low"])
        hi = float(row["high"])
        # Conservative intrabar assumption for simultaneous hit.
        if lo <= sl_opt:
            return "LOSS", i, float(sl_opt), "SL_HIT"
        if hi >= tp3_opt:
            return "WIN", i, float(tp3_opt), "TP3_HIT"
    last_px = float(opt_df.iloc[end_idx]["close"])
    return "BREAKEVEN", end_idx, last_px, "TIMEOUT"


def _fmt_ts(ts: datetime) -> str:
    return ts.astimezone(IST).isoformat()


def _signal_minutes(signal_interval: str) -> int:
    s = str(signal_interval or "5minute").strip().lower().replace(" ", "")
    if s in ("1minute", "1m", "1min", "minute"):
        return 1
    if s in ("3minute", "3m", "3min"):
        return 3
    if s in ("5minute", "5m", "5min"):
        return 5
    if s in ("15minute", "15m", "15min"):
        return 15
    if s in ("30minute", "30m", "30min"):
        return 30
    if s in ("60minute", "60m", "60min", "1hour", "1h"):
        return 60
    return 5


def run_backtest(
    user: str,
    days: int,
    max_hold_bars: int,
    signal_interval: str,
) -> tuple[list[TradeResult], list[str]]:
    creds = load_zerodha_credentials_for_user(user)
    api_key = (creds.get("api_key") or "").strip()
    access_token = (creds.get("access_token") or "").strip()
    if not api_key or not access_token:
        raise RuntimeError(f"Missing Kite credentials for user={user}")

    cfg = TRADING_CONFIG
    scripts = [s for s in (cfg.get("options_scripts") or []) if s in {"NIFTY", "BANKNIFTY", "SENSEX"}]
    signal_interval = str(signal_interval or "5minute")
    kite_iv = map_bot_interval_to_kite(signal_interval)
    mins = _signal_minutes(signal_interval)
    short = int(cfg.get("ema_short", 5))
    long = int(cfg.get("ema_long", 18))
    adx_min = float(cfg.get("options_standalone_adx_min", 20.0))
    rsi_period = int(cfg.get("options_standalone_rsi_period", 14))
    rsi_min_call = float(cfg.get("options_standalone_rsi_min_call", 55.0))
    rsi_max_put = float(cfg.get("options_standalone_rsi_max_put", 45.0))
    iv = float(cfg.get("options_iv_annual", 0.18))
    rfr = float(cfg.get("options_risk_free_rate", 0.0))

    now = datetime.now(IST)
    from_dt = now - timedelta(days=max(2, int(days)))
    to_dt = now

    nfo_csv = _fetch_instruments_csv(api_key, access_token, "NFO")
    bfo_csv = _fetch_instruments_csv(api_key, access_token, "BFO")
    option_rows = {
        s: (_parse_option_master(nfo_csv, s) + _parse_option_master(bfo_csv, s)) for s in scripts
    }
    opt_cache: dict[int, pd.DataFrame] = {}
    results: list[TradeResult] = []
    warnings: list[str] = []

    def _fetch_with_cont_fallback(
        token: int,
        *,
        interval: str,
        from_dt_: datetime,
        to_dt_: datetime,
        prefer_cont: str,
        oi: str = "0",
    ) -> list[list]:
        tries = [prefer_cont]
        if prefer_cont != "0":
            tries.append("0")
        last_err: Exception | None = None
        for cont in tries:
            try:
                return fetch_historical_raw(
                    api_key,
                    access_token,
                    token,
                    interval,
                    from_dt_,
                    to_dt_,
                    continuous=cont,
                    oi=oi,
                )
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue
        if last_err is not None:
            raise last_err
        return []

    def _fetch_chunked(
        token: int,
        *,
        interval: str,
        from_dt_: datetime,
        to_dt_: datetime,
        prefer_cont: str,
        oi: str = "0",
        chunk_days: int = 3,
        max_retries: int = 4,
    ) -> list[list]:
        out: list[list] = []
        cur = from_dt_
        while cur < to_dt_:
            end = min(to_dt_, cur + timedelta(days=max(1, int(chunk_days))))
            rows: list[list] | None = None
            last_err: Exception | None = None
            for attempt in range(max_retries):
                try:
                    rows = _fetch_with_cont_fallback(
                        token,
                        interval=interval,
                        from_dt_=cur,
                        to_dt_=end,
                        prefer_cont=prefer_cont,
                        oi=oi,
                    )
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    # exponential-ish backoff: 1.5s, 3s, 6s, 12s
                    pytime.sleep(1.5 * (2 ** attempt))
            if rows is None:
                if last_err is not None:
                    raise last_err
                rows = []
            out.extend(rows)
            # small pause to reduce burst/rate pressure on Kite historical endpoint
            pytime.sleep(0.25)
            cur = end
        return out

    for script in scripts:
        tok = resolve_kite_instrument_token(script, api_key, access_token)
        if not tok:
            warnings.append(f"{script}: unable to resolve Kite futures token")
            continue
        try:
            rows = _fetch_chunked(
                tok,
                interval=kite_iv,
                from_dt_=from_dt,
                to_dt_=to_dt,
                prefer_cont="1",
                oi="0",
            )
        except Exception as e:
            warnings.append(f"{script}: underlying fetch failed ({e})")
            continue

        udf = kite_candles_to_dataframe(rows)
        if udf is None or udf.empty:
            warnings.append(f"{script}: underlying candles empty")
            continue
        udf = _coerce_ohlcv_numeric(udf)
        udf = _resample(udf, mins)
        udf = _coerce_ohlcv_numeric(udf)
        if len(udf) < long + 5:
            warnings.append(f"{script}: insufficient candles after resample ({len(udf)})")
            continue
        udf = _calculate_signals(udf, short, long)
        adx, _, _ = _calculate_adx_values(udf, int(cfg.get("adx_period", 14)))
        udf["adx"] = adx
        udf["vwap"] = _calculate_session_vwap(udf)
        udf["rsi"] = _calculate_rsi(udf["close"], rsi_period)

        rows_opt_master = option_rows.get(script) or []
        if not rows_opt_master:
            warnings.append(f"{script}: no option rows in NFO/BFO option master")
            continue

        for i in range(max(long, 3), len(udf) - 1):
            row = udf.iloc[i]
            sig = int(row["signal"])
            cross = bool(row["crossover"])
            if not cross or sig not in (1, -1):
                continue
            adx_v = float(row["adx"])
            if adx_v < adx_min:
                continue
            vol = float(row["volume"])
            if vol <= max(float(udf.iloc[i - 1]["volume"]), float(udf.iloc[i - 2]["volume"])):
                continue
            close_u = float(row["close"])
            vwap = float(row["vwap"]) if pd.notna(row["vwap"]) else float("nan")
            if not math.isfinite(vwap):
                continue
            if sig == 1 and not (close_u > vwap):
                continue
            if sig == -1 and not (close_u < vwap):
                continue
            rsi = float(row["rsi"]) if pd.notna(row["rsi"]) else float("nan")
            if not math.isfinite(rsi):
                continue
            if sig == 1 and rsi <= rsi_min_call:
                continue
            if sig == -1 and rsi >= rsi_max_put:
                continue

            side = "BUY" if sig == 1 else "SELL"
            opt_type = "CE" if sig == 1 else "PE"
            sl_u = _get_entry_swing_sl(udf, i, side, long)
            if sl_u is None:
                continue
            if side == "BUY" and not (float(sl_u) < close_u):
                continue
            if side == "SELL" and not (float(sl_u) > close_u):
                continue

            ts_u = pd.to_datetime(row["timestamp"]).to_pydatetime()
            if ts_u.tzinfo is None:
                ts_u = ts_u.replace(tzinfo=IST)
            opt_row = _pick_atm_option(script, rows_opt_master, ts_u.date(), close_u, opt_type)
            if not opt_row:
                continue

            token = int(opt_row["token"])
            if token not in opt_cache:
                try:
                    o_rows = _fetch_chunked(
                        token,
                        interval=kite_iv,
                        from_dt_=from_dt - timedelta(days=2),
                        to_dt_=to_dt + timedelta(days=1),
                        prefer_cont="0",
                        oi="0",
                    )
                except Exception:
                    opt_cache[token] = pd.DataFrame()
                    continue
                odf = kite_candles_to_dataframe(o_rows)
                if odf is None or odf.empty:
                    opt_cache[token] = pd.DataFrame()
                else:
                    opt_cache[token] = _coerce_ohlcv_numeric(_resample(_coerce_ohlcv_numeric(odf), mins))

            odf = opt_cache.get(token)
            if odf is None or odf.empty:
                continue
            ots = pd.to_datetime(odf["timestamp"], errors="coerce")
            le = odf[ots <= pd.Timestamp(ts_u)]
            if le.empty:
                continue
            oi_idx = int(le.index[-1])
            entry_opt = float(le.iloc[-1]["close"])
            if entry_opt <= 0:
                continue

            prem_r = _premium_r(
                fut_entry=close_u,
                fut_sl=float(sl_u),
                strike=float(opt_row["strike"]),
                expiry=opt_row["expiry"],
                opt_type=opt_type,
                iv=iv,
                rfr=rfr,
            )
            sl_opt = float(entry_opt) - float(prem_r)
            tp3_opt = float(entry_opt) + (3.0 * float(prem_r))
            outcome, ex_idx, ex_px, why = _simulate_option_trade(
                odf, oi_idx, entry_opt, sl_opt, tp3_opt, max_hold_bars
            )
            ex_ts = pd.to_datetime(odf.iloc[ex_idx]["timestamp"]).to_pydatetime()
            if ex_ts.tzinfo is None:
                ex_ts = ex_ts.replace(tzinfo=IST)

            results.append(
                TradeResult(
                    script=script,
                    option_symbol=str(opt_row["symbol"]),
                    side=opt_type,
                    entry_ts=ts_u,
                    exit_ts=ex_ts,
                    entry_underlying=float(close_u),
                    entry_option=float(entry_opt),
                    sl_option=float(sl_opt),
                    tp3_option=float(tp3_opt),
                    exit_option=float(ex_px),
                    outcome=outcome,
                    reason=why,
                    strike=float(opt_row["strike"]),
                    expiry=str(opt_row["expiry"]),
                )
            )

    return results, warnings


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest standalone ATM options logic on Kite historical data.")
    ap.add_argument("--user", default="AK07")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--max-hold-bars", type=int, default=48, help="Timeout horizon in signal bars")
    ap.add_argument(
        "--signal-interval",
        default="5minute",
        help="Backtest strategy timeframe (default 5minute).",
    )
    ap.add_argument(
        "--csv-out",
        default=str(REPO_ROOT / "tmp" / "options_standalone_backtest.csv"),
    )
    args = ap.parse_args()

    out, warns = run_backtest(
        args.user,
        args.days,
        args.max_hold_bars,
        args.signal_interval,
    )
    Path(args.csv_out).parent.mkdir(parents=True, exist_ok=True)

    with open(args.csv_out, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(
            [
                "script",
                "option_symbol",
                "side",
                "entry_ts",
                "exit_ts",
                "entry_underlying",
                "entry_option",
                "sl_option",
                "tp3_option",
                "exit_option",
                "outcome",
                "reason",
                "pnl_points",
                "r_multiple",
                "strike",
                "expiry",
            ]
        )
        for r in out:
            wr.writerow(
                [
                    r.script,
                    r.option_symbol,
                    r.side,
                    _fmt_ts(r.entry_ts),
                    _fmt_ts(r.exit_ts),
                    f"{r.entry_underlying:.2f}",
                    f"{r.entry_option:.2f}",
                    f"{r.sl_option:.2f}",
                    f"{r.tp3_option:.2f}",
                    f"{r.exit_option:.2f}",
                    r.outcome,
                    r.reason,
                    f"{_trade_pnl_points(r):.2f}",
                    f"{_trade_r_multiple(r):.2f}",
                    f"{r.strike:.2f}",
                    r.expiry,
                ]
            )

    wins = sum(1 for r in out if r.outcome == "WIN")
    loss = sum(1 for r in out if r.outcome == "LOSS")
    be = sum(1 for r in out if r.outcome == "BREAKEVEN")
    total_pnl = sum(_trade_pnl_points(r) for r in out)
    avg_pnl = (total_pnl / len(out)) if out else 0.0
    total_r = sum(_trade_r_multiple(r) for r in out)
    timeout_rows = [r for r in out if r.reason == "TIMEOUT"]
    timeout_pos = sum(1 for r in timeout_rows if _trade_pnl_points(r) > 0)
    timeout_neg = sum(1 for r in timeout_rows if _trade_pnl_points(r) < 0)
    timeout_flat = sum(1 for r in timeout_rows if abs(_trade_pnl_points(r)) < 1e-9)
    timeout_pnl = sum(_trade_pnl_points(r) for r in timeout_rows)
    by_script: dict[str, dict[str, float]] = {}
    by_reason: dict[str, dict[str, float]] = {}
    for r in out:
        pnl = _trade_pnl_points(r)
        s = by_script.setdefault(r.script, {"trades": 0.0, "pnl": 0.0, "wins": 0.0, "losses": 0.0, "timeouts": 0.0})
        s["trades"] += 1.0
        s["pnl"] += pnl
        if r.outcome == "WIN":
            s["wins"] += 1.0
        elif r.outcome == "LOSS":
            s["losses"] += 1.0
        elif r.outcome == "BREAKEVEN":
            s["timeouts"] += 1.0

        rr = by_reason.setdefault(r.reason, {"trades": 0.0, "pnl": 0.0})
        rr["trades"] += 1.0
        rr["pnl"] += pnl

    print(
        f"Standalone options backtest complete | user={args.user} | days={args.days} "
        f"| signal_interval={args.signal_interval}"
    )
    print(f"Trades={len(out)} | Wins={wins} | Losses={loss} | Breakeven={be}")
    print(
        f"PnL(points, qty=1)={total_pnl:.2f} | Avg/Trade={avg_pnl:.2f} | Total_R={total_r:.2f}"
    )
    print(
        f"Timeout trades={len(timeout_rows)} | Timeout PnL={timeout_pnl:.2f} "
        f"| Timeout +/−/0 = {timeout_pos}/{timeout_neg}/{timeout_flat}"
    )
    if out:
        print("\nPnL by script:")
        for script in sorted(by_script.keys()):
            s = by_script[script]
            print(
                f"- {script}: trades={int(s['trades'])} | pnl={s['pnl']:.2f} | "
                f"wins/losses/timeouts={int(s['wins'])}/{int(s['losses'])}/{int(s['timeouts'])}"
            )
        print("\nPnL by exit reason:")
        for reason in sorted(by_reason.keys()):
            rr = by_reason[reason]
            print(f"- {reason}: trades={int(rr['trades'])} | pnl={rr['pnl']:.2f}")
    print(f"CSV={args.csv_out}")
    if warns:
        print("\nWarnings:")
        for w in warns:
            print(f"- {w}")
    print("\nSample (first 10 trades):")
    for r in out[:10]:
        print(
            f"{_fmt_ts(r.entry_ts)} | {r.script} {r.side} {r.option_symbol} "
            f"-> {_fmt_ts(r.exit_ts)} | {r.outcome} ({r.reason}) "
            f"| pnl={_trade_pnl_points(r):.2f} | R={_trade_r_multiple(r):.2f}"
        )


if __name__ == "__main__":
    main()

