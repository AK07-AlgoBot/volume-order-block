"""Historical market data for AK07 backtests (Upstox V3, disk cache)."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote
from zoneinfo import ZoneInfo

from app.config.paths import server_root
from app.services.upstox_engine import UpstoxClient, _parse_v3_candle_row

logger = logging.getLogger("ak07.backtest_data")

IST: Final = ZoneInfo("Asia/Kolkata")
CACHE_DIR: Final[Path] = server_root() / "data" / "backtest_cache"


def parse_historical_candles(data: Any) -> list[dict[str, float]]:
    if not isinstance(data, dict):
        return []
    rows = data.get("candles")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, float]] = []
    for row in rows:
        candle = _parse_v3_candle_row(row)
        if candle is not None:
            out.append(candle)
    out.sort(key=lambda c: c["timestamp"])
    return out


def parse_candle_ts(raw: str) -> datetime:
    ts = datetime.fromisoformat(str(raw))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    return ts.astimezone(IST)


class HistoricalDataClient:
    """Fetch and cache Upstox historical candles for backtesting."""

    def __init__(self, username: str = "AK07") -> None:
        self.client = UpstoxClient(username=username)
        if not self.client.refresh_access_token_from_disk():
            raise RuntimeError(
                "No Upstox access_token in src/server/data/users/AK07/upstox_credentials.json - "
                "add your token before running backtests."
            )
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _v3_base(self) -> str:
        return self.client.base_url.replace("/v2", "/v3")

    def _cache_path(self, instrument_key: str, unit: str, interval: str, start: date, end: date) -> Path:
        safe = instrument_key.replace("|", "_").replace(" ", "_")
        return CACHE_DIR / safe / f"{unit}_{interval}_{start.isoformat()}_{end.isoformat()}.json"

    def _load_cache(self, path: Path) -> list[dict[str, float]] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = payload.get("candles")
            if isinstance(rows, list):
                return rows
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Cache read failed %s: %s", path, exc)
        return None

    def _save_cache(self, path: Path, candles: list[dict[str, float]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"candles": candles}, indent=0), encoding="utf-8")

    def _max_chunk_days(self, unit: str, interval: str) -> int:
        """Upstox V3 max retrieval window per request."""
        if unit == "minutes":
            try:
                mins = int(interval)
            except ValueError:
                mins = 5
            return 28 if mins <= 15 else 90
        if unit == "hours":
            return 90
        if unit in ("days", "weeks", "months"):
            return 3650
        return 28

    def _date_chunks(self, start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
        if start > end:
            return []
        chunks: list[tuple[date, date]] = []
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=chunk_days - 1))
            chunks.append((cursor, chunk_end))
            cursor = chunk_end + timedelta(days=1)
        return chunks

    def _fetch_candles_once(
        self,
        instrument_key: str,
        *,
        unit: str,
        interval: str,
        start: date,
        end: date,
    ) -> list[dict[str, float]]:
        key = quote(instrument_key, safe="")
        url = f"{self._v3_base()}/historical-candle/{key}/{unit}/{interval}/{end.isoformat()}/{start.isoformat()}"
        data = self.client._get(url)  # noqa: SLF001
        candles = parse_historical_candles(data)
        if not candles and unit == "hours":
            v2_url = (
                f"{self.client.base_url}/historical-candle/{key}/{unit.rstrip('s')}/"
                f"{end.isoformat()}/{start.isoformat()}"
            )
            candles = parse_historical_candles(self.client._get(v2_url))  # noqa: SLF001
        if not candles and unit == "days":
            v2_url = (
                f"{self.client.base_url}/historical-candle/{key}/day/"
                f"{end.isoformat()}/{start.isoformat()}"
            )
            candles = parse_historical_candles(self.client._get(v2_url))  # noqa: SLF001
        return candles

    def fetch_candles(
        self,
        instrument_key: str,
        *,
        unit: str,
        interval: str,
        start: date,
        end: date,
        use_cache: bool = True,
    ) -> list[dict[str, float]]:
        """Fetch historical candles; `end` is the newest date in the range."""
        cache_path = self._cache_path(instrument_key, unit, interval, start, end)
        if use_cache:
            cached = self._load_cache(cache_path)
            if cached is not None:
                return cached

        chunk_days = self._max_chunk_days(unit, interval)
        merged: dict[str, dict[str, float]] = {}
        for chunk_start, chunk_end in self._date_chunks(start, end, chunk_days):
            rows = self._fetch_candles_once(
                instrument_key,
                unit=unit,
                interval=interval,
                start=chunk_start,
                end=chunk_end,
            )
            for row in rows:
                merged[row["timestamp"]] = row

        candles = sorted(merged.values(), key=lambda c: c["timestamp"])
        if use_cache and candles:
            self._save_cache(cache_path, candles)
        return candles

    def fetch_5m(self, instrument_key: str, start: date, end: date, *, use_cache: bool = True) -> list[dict[str, float]]:
        return self.fetch_candles(instrument_key, unit="minutes", interval="5", start=start, end=end, use_cache=use_cache)

    def fetch_1h(self, instrument_key: str, start: date, end: date, *, use_cache: bool = True) -> list[dict[str, float]]:
        return self.fetch_candles(instrument_key, unit="hours", interval="1", start=start, end=end, use_cache=use_cache)

    def fetch_daily(self, instrument_key: str, start: date, end: date, *, use_cache: bool = True) -> list[dict[str, float]]:
        return self.fetch_candles(instrument_key, unit="days", interval="1", start=start, end=end, use_cache=use_cache)

    @staticmethod
    def prior_session_ohlc(daily: list[dict[str, float]], session_day: date) -> dict[str, float] | None:
        best: dict[str, float] | None = None
        best_day: date | None = None
        for candle in daily:
            ts = parse_candle_ts(candle["timestamp"])
            row_day = ts.date()
            if row_day >= session_day:
                continue
            if best_day is None or row_day > best_day:
                best_day = row_day
                best = {
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                    "date": row_day.isoformat(),
                }
        return best

    @staticmethod
    def session_5m(candles_5m: list[dict[str, float]], session_day: date) -> list[dict[str, float]]:
        out: list[dict[str, float]] = []
        for candle in candles_5m:
            ts = parse_candle_ts(candle["timestamp"])
            if ts.date() == session_day:
                out.append(candle)
        out.sort(key=lambda c: c["timestamp"])
        return out

    @staticmethod
    def trading_days(candles_5m: list[dict[str, float]], start: date, end: date) -> list[date]:
        days: set[date] = set()
        for candle in candles_5m:
            ts = parse_candle_ts(candle["timestamp"])
            if start <= ts.date() <= end:
                days.add(ts.date())
        return sorted(days)

    @staticmethod
    def closed_bars(candles: list[dict[str, float]], as_of: datetime, minutes: int = 5) -> list[dict[str, float]]:
        closed: list[dict[str, float]] = []
        for candle in candles:
            ts = parse_candle_ts(candle["timestamp"])
            if ts + timedelta(minutes=minutes) <= as_of:
                closed.append(candle)
        return closed

    @staticmethod
    def daily_row(candle: dict[str, float]) -> dict[str, float]:
        ts = parse_candle_ts(candle["timestamp"])
        return {
            "date": ts.date().isoformat(),
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
        }
