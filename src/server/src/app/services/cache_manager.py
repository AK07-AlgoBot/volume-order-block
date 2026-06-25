"""AK07 in-memory cache pipeline (Redis).

Asynchronous Observer Architecture bridge: the trading loop publishes live
market snapshots here, and external observers (local AI reasoning) publish a
system bias back. Every public function is fail-safe: a Redis outage or bad
payload is logged and absorbed so the main execution thread never crashes
because of a caching failure.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Final

import redis
from redis.exceptions import RedisError

logger = logging.getLogger("ak07.cache_manager")

REDIS_HOST: Final[str] = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT: Final[int] = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_DB: Final[int] = int(os.environ.get("REDIS_DB", "0"))

LIVE_STATE_KEY: Final[str] = "ak07:live_state"
SYSTEM_MODE_KEY: Final[str] = "ak07:system_mode"

# Additive keys for the multi-index engine / dashboard (existing schema unchanged).
INDEX_STATE_KEY_TEMPLATE: Final[str] = "ak07:index_state:{index}"
POSITIONS_KEY: Final[str] = "ak07:positions"
KILL_SWITCH_KEY: Final[str] = "ak07:kill_switch"
DAILY_PROFIT_TARGET_KEY: Final[str] = "ak07:daily_profit_target"
UPSTOX_DAILY_PNL_KEY: Final[str] = "ak07:upstox_daily_pnl"
ENGINE_HEARTBEAT_KEY: Final[str] = "ak07:engine_heartbeat"
SMC_CRT_STATE_KEY_TEMPLATE: Final[str] = "ak07:smc_crt_state:{symbol}"
SMC_CRT_HEARTBEAT_KEY: Final[str] = "ak07:smc_crt_heartbeat"
BREAKOUT_STATE_KEY_TEMPLATE: Final[str] = "ak07:breakout_state:{index}"
BREAKOUT_FROZEN_KEY_TEMPLATE: Final[str] = "ak07:breakout_frozen:{day}:{index}"
BREAKOUT_HEARTBEAT_KEY: Final[str] = "ak07:breakout_heartbeat"
S7_STATE_KEY: Final[str] = "ak07:s7_state"
CHOCH_STATE_KEY: Final[str] = "ak07:choch_state"
CHOCH_SESSION_KEY_TEMPLATE: Final[str] = "ak07:choch_session:{day}"
GAMMA_STATE_KEY: Final[str] = "ak07:gamma_state"
GAMMA_HEARTBEAT_KEY: Final[str] = "ak07:gamma_heartbeat"
GAMMA_BACKTEST_KEY: Final[str] = "ak07:gamma_backtest_summary"
TRADE_LOG_KEY_TEMPLATE: Final[str] = "ak07:trade_log:{day}"

LIVE_STATE_TTL_SECONDS: Final[int] = 300

DEFAULT_BIAS: Final[str] = "NEUTRAL"
VALID_BIASES: Final[frozenset[str]] = frozenset({"LONG_ONLY", "SHORT_ONLY", "NEUTRAL"})

SNAPSHOT_FIELDS: Final[dict[str, type | tuple[type, ...]]] = {
    "spot_price": (int, float),
    "volume": int,
    "highest_call_oi_strike": int,
    "highest_put_oi_strike": int,
    "timestamp": str,
}

_pool: redis.ConnectionPool | None = None
_mock_client: Any = None


def _mock_mode() -> bool:
    """AK07_MOCK=1 swaps Redis for an in-process fakeredis (cockpit demos)."""
    return os.environ.get("AK07_MOCK") == "1"


def _get_pool() -> redis.ConnectionPool:
    global _pool
    if _pool is None:
        _pool = redis.ConnectionPool(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            health_check_interval=30,
        )
        logger.info("Redis connection pool initialized (%s:%d/db%d)", REDIS_HOST, REDIS_PORT, REDIS_DB)
    return _pool


def _get_client() -> redis.Redis | None:
    """Return a pooled Redis client, or None if Redis is unreachable."""
    if _mock_mode():
        global _mock_client
        if _mock_client is None:
            try:
                import fakeredis  # noqa: PLC0415

                _mock_client = fakeredis.FakeRedis(decode_responses=True)
                logger.info("AK07_MOCK=1 -> using in-process fakeredis (no real Redis required)")
            except ImportError:
                logger.error("AK07_MOCK=1 but fakeredis is not installed (pip install fakeredis)")
                return None
        return _mock_client
    try:
        client = redis.Redis(connection_pool=_get_pool())
        client.ping()
        return client
    except (RedisError, OSError) as exc:
        logger.error("Redis unreachable at %s:%d: %s", REDIS_HOST, REDIS_PORT, exc)
        return None


def _validate_snapshot(data: dict[str, Any]) -> str | None:
    """Return an error description if the snapshot is malformed, else None."""
    if not isinstance(data, dict):
        return f"snapshot must be a dict, got {type(data).__name__}"
    for field, expected in SNAPSHOT_FIELDS.items():
        if field not in data:
            return f"missing field '{field}'"
        if isinstance(data[field], bool) or not isinstance(data[field], expected):
            return f"field '{field}' has invalid type {type(data[field]).__name__}"
    return None


def set_market_snapshot(data: dict[str, Any]) -> bool:
    """Serialize and store the live market snapshot with a 300s TTL.

    Returns True on success, False on any failure (never raises).
    """
    try:
        error = _validate_snapshot(data)
        if error is not None:
            logger.error("Rejected market snapshot: %s", error)
            return False

        client = _get_client()
        if client is None:
            return False

        payload = json.dumps(
            {field: data[field] for field in SNAPSHOT_FIELDS},
            ensure_ascii=False,
        )
        client.set(LIVE_STATE_KEY, payload, ex=LIVE_STATE_TTL_SECONDS)
        logger.info(
            "Market snapshot cached: spot=%s vol=%s ce_oi_strike=%s pe_oi_strike=%s ts=%s",
            data["spot_price"],
            data["volume"],
            data["highest_call_oi_strike"],
            data["highest_put_oi_strike"],
            data["timestamp"],
        )
        return True
    except Exception:  # noqa: BLE001 - caching must never crash the trading loop
        logger.exception("Unexpected failure while caching market snapshot")
        return False


def get_market_snapshot() -> dict[str, Any] | None:
    """Read back the cached snapshot, or None if absent/expired/unreachable."""
    try:
        client = _get_client()
        if client is None:
            return None
        raw = client.get(LIVE_STATE_KEY)
        if raw is None:
            return None
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected failure while reading market snapshot")
        return None


def set_json(key: str, value: Any, ttl_seconds: int | None = None) -> bool:
    """Serialize any JSON-able value under an arbitrary ak07 key (fail-safe)."""
    try:
        client = _get_client()
        if client is None:
            return False
        client.set(key, json.dumps(value, ensure_ascii=False), ex=ttl_seconds)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected failure while writing key %s", key)
        return False


def get_json(key: str) -> Any | None:
    """Read and decode a JSON value, or None if absent/invalid/unreachable."""
    try:
        client = _get_client()
        if client is None:
            return None
        raw = client.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected failure while reading key %s", key)
        return None


def delete_key(key: str) -> bool:
    """Delete a key (fail-safe)."""
    try:
        client = _get_client()
        if client is None:
            return False
        client.delete(key)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected failure while deleting key %s", key)
        return False


def get_system_bias() -> str:
    """Read the current system bias; defaults to NEUTRAL on any failure."""
    try:
        client = _get_client()
        if client is None:
            return DEFAULT_BIAS
        value = client.get(SYSTEM_MODE_KEY)
        if value is None:
            return DEFAULT_BIAS
        bias = str(value).strip().upper()
        if bias not in VALID_BIASES:
            logger.warning("Unknown system bias %r in Redis; defaulting to %s", value, DEFAULT_BIAS)
            return DEFAULT_BIAS
        return bias
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected failure while reading system bias")
        return DEFAULT_BIAS


def set_system_bias(mode: str) -> bool:
    """Write the system bias (LONG_ONLY / SHORT_ONLY / NEUTRAL).

    Returns True on success, False on any failure (never raises).
    """
    try:
        normalized = str(mode).strip().upper()
        if normalized not in VALID_BIASES:
            logger.error("Rejected system bias %r; must be one of %s", mode, sorted(VALID_BIASES))
            return False

        client = _get_client()
        if client is None:
            return False

        client.set(SYSTEM_MODE_KEY, normalized)
        logger.info("System bias updated: %s", normalized)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected failure while writing system bias")
        return False
