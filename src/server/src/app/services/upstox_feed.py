"""Upstox V3 Market-Data WebSocket feed (live tick source).

Purpose
-------
Stream live ticks (``ltpc`` mode) over a single WebSocket connection and write
each tick into ``UpstoxClient._ltp_cache`` so that ``get_ltp()`` serves tick
prices instead of polling the REST ``market-quote/ltp`` endpoint. REST polling is
what exhausts the account-wide Upstox quota and produces the HTTP 429s that stall
option SL/TP/trailing.

Safety
------
This module can only ever ADD fresh prices to the existing cache. If anything
about the feed is unavailable — the SDK isn't installed, the socket drops, the
token is stale — ``get_ltp`` transparently falls back to the REST path exactly as
before. So enabling the feed can never make price retrieval worse than the
REST-only baseline. Kill switch: ``AK07_WS_FEED=0``.

Wiring lives in ``upstox_engine.UpstoxClient`` (lazy: the first ``get_ltp`` for a
key starts the feed and subscribes that key).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger("ak07.upstox_feed")

# Cloudflare (in front of api.upstox.com) bans bot-signature User-Agents such as
# python-urllib / python-httpx. Present a browser signature for the WS handshake.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _extract_feeds(message: object) -> dict:
    """Return the ``feeds`` mapping from an SDK message (dict or protobuf)."""
    if isinstance(message, dict):
        feeds = message.get("feeds")
        return feeds if isinstance(feeds, dict) else {}
    try:  # protobuf message object -> dict
        from google.protobuf.json_format import MessageToDict  # noqa: PLC0415

        decoded = MessageToDict(message, preserving_proto_field_name=True)
        feeds = decoded.get("feeds")
        return feeds if isinstance(feeds, dict) else {}
    except Exception:  # noqa: BLE001 - never let decode issues bubble into trading
        return {}


def _ltp_from_node(node: object) -> float | None:
    """Pull the last-traded price out of a per-instrument feed node.

    Handles ``ltpc`` mode (``node.ltpc.ltp``) and, defensively, the ``full`` /
    ``full_d30`` modes that nest ltpc under ``fullFeed.{marketFF,indexFF}``.
    """
    if not isinstance(node, dict):
        return None
    ltpc = node.get("ltpc")
    if isinstance(ltpc, dict) and ltpc.get("ltp") is not None:
        try:
            return float(ltpc["ltp"])
        except (TypeError, ValueError):
            return None
    full = node.get("fullFeed") or node.get("full_feed")
    if isinstance(full, dict):
        for sub in ("marketFF", "indexFF", "market_ff", "index_ff"):
            block = full.get(sub)
            if isinstance(block, dict):
                inner = block.get("ltpc")
                if isinstance(inner, dict) and inner.get("ltp") is not None:
                    try:
                        return float(inner["ltp"])
                    except (TypeError, ValueError):
                        return None
    return None


class UpstoxMarketFeed:
    """Process-wide singleton that maintains one V3 market-data WebSocket."""

    _instance: "UpstoxMarketFeed | None" = None
    _guard = threading.Lock()

    def __init__(
        self,
        token_provider: Callable[[], str],
        cache_writer: Callable[[str, float], None],
    ) -> None:
        self._token_provider = token_provider
        self._cache_writer = cache_writer
        self._streamer = None
        self._want: set[str] = set()
        self._connected = False
        self._started = False
        self._last_tick_mono = 0.0
        self._lock = threading.Lock()

    @classmethod
    def get(
        cls,
        token_provider: Callable[[], str],
        cache_writer: Callable[[str, float], None],
    ) -> "UpstoxMarketFeed":
        with cls._guard:
            if cls._instance is None:
                cls._instance = cls(token_provider, cache_writer)
            return cls._instance

    # ------------------------------------------------------------------ API
    def ensure_started(self) -> None:
        """Start the background connect thread once (idempotent)."""
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._boot, name="upstox-feed", daemon=True).start()

    def want(self, instrument_key: str) -> None:
        """Register interest in an instrument; subscribe immediately if live."""
        if not instrument_key:
            return
        with self._lock:
            is_new = instrument_key not in self._want
            self._want.add(instrument_key)
        if is_new and self._connected:
            self._safe_subscribe([instrument_key])

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_tick_age(self) -> float:
        if self._last_tick_mono <= 0:
            return float("inf")
        return time.monotonic() - self._last_tick_mono

    # ------------------------------------------------------------- internals
    def _boot(self) -> None:
        try:
            import upstox_client  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "upstox-python-sdk unavailable (%s); WS feed OFF, staying on REST", exc
            )
            self._started = False
            return
        try:
            token = (self._token_provider() or "").strip()
            if not token:
                logger.warning("WS feed: no access token yet; will retry on next get_ltp")
                self._started = False
                return

            config = upstox_client.Configuration()
            config.access_token = token
            api_client = upstox_client.ApiClient(config)
            try:
                api_client.default_headers["User-Agent"] = _BROWSER_UA
            except Exception:  # noqa: BLE001
                pass

            with self._lock:
                init_keys = sorted(self._want) or ["NSE_INDEX|Nifty 50"]

            streamer = upstox_client.MarketDataStreamerV3(api_client, init_keys, "ltpc")
            streamer.on("open", self._on_open)
            streamer.on("message", self._on_message)
            streamer.on("error", self._on_error)
            streamer.on("close", self._on_close)
            try:
                streamer.auto_reconnect(True, 5, 20)
            except Exception:  # noqa: BLE001 - older SDKs may lack tuning
                pass
            self._streamer = streamer
            logger.info(
                "Upstox WS feed connecting (ltpc, %d key[s]) — REST LTP polling now standby",
                len(init_keys),
            )
            streamer.connect()  # spawns the SDK feeder thread; callbacks fire async
        except Exception as exc:  # noqa: BLE001
            logger.warning("WS feed boot failed (%s); staying on REST", exc)
            self._started = False

    def _on_open(self, *_args) -> None:
        self._connected = True
        with self._lock:
            keys = sorted(self._want)
        if keys:
            self._safe_subscribe(keys)
        logger.info("Upstox WS feed connected; streaming %d key(s)", len(keys))

    def _on_close(self, *_args) -> None:
        self._connected = False
        logger.warning("Upstox WS feed closed; REST fallback active until reconnect")

    def _on_error(self, *args) -> None:
        logger.warning("Upstox WS feed error: %s", args[0] if args else "")

    def _on_message(self, message: object) -> None:
        feeds = _extract_feeds(message)
        if not feeds:
            return
        self._last_tick_mono = time.monotonic()
        for key, node in feeds.items():
            ltp = _ltp_from_node(node)
            if ltp is not None and ltp > 0:
                try:
                    self._cache_writer(key, ltp)
                except Exception:  # noqa: BLE001 - a bad tick must not kill the feed
                    pass

    def _safe_subscribe(self, keys: list[str]) -> None:
        streamer = self._streamer
        if streamer is None or not keys:
            return
        try:
            streamer.subscribe(keys, "ltpc")
        except Exception as exc:  # noqa: BLE001
            logger.warning("WS subscribe failed for %s: %s", keys, exc)
