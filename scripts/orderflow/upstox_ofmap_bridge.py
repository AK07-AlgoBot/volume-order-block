#!/usr/bin/env python3
"""Upstox full-feed → OrderFlowMap Live WebSocket bridge.

Speaks the same client protocol OrderFlowMap expects (authenticate / subscribe /
market_data) so Live mode works without OpenAlgo.

Default listen: ws://127.0.0.1:8766  (8765 is reserved for AK07 MCP)

Usage:
  python scripts/orderflow/upstox_ofmap_bridge.py
  python scripts/orderflow/upstox_ofmap_bridge.py --user AK07 --port 8766

OrderFlowMap UI:
  Live → WS URL ws://127.0.0.1:8766 → API key ak07 → Symbol NIFTY → Exchange NFO
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src" / "lib"))

from upstox_credentials_store import (  # noqa: E402
    load_upstox_credentials_for_user,
    normalize_access_token,
)

logger = logging.getLogger("ak07.ofmap_bridge")

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# OrderFlowMap exchange code → Upstox search
_EXCHANGE_MAP = {
    "NFO": ("NSE", "FO"),
    "NSE": ("NSE", "EQ"),
    "BFO": ("BSE", "FO"),
    "BSE": ("BSE", "EQ"),
    "MCX": ("MCX", "COM"),
}

_INDEX_ALIASES = {
    "NIFTY": "NIFTY",
    "NIFTY-I": "NIFTY",
    "NIFTY50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "BANKNIFTY-I": "BANKNIFTY",
    "BN": "BANKNIFTY",
    "SENSEX": "SENSEX",
}


def _extract_feeds(message: object) -> dict:
    if isinstance(message, dict):
        feeds = message.get("feeds")
        return feeds if isinstance(feeds, dict) else {}
    try:
        from google.protobuf.json_format import MessageToDict

        decoded = MessageToDict(message, preserving_proto_field_name=True)
        feeds = decoded.get("feeds")
        return feeds if isinstance(feeds, dict) else {}
    except Exception:
        return {}


def _market_ff(node: dict) -> dict | None:
    full = node.get("fullFeed") or node.get("full_feed") or {}
    if not isinstance(full, dict):
        return None
    for key in ("marketFF", "market_ff", "indexFF", "index_ff"):
        block = full.get(key)
        if isinstance(block, dict):
            return block
    # Some SDK builds nest under FullFeedUnion
    return full if full.get("ltpc") or full.get("marketLevel") else None


def _to_ofmap_payload(node: dict) -> dict[str, Any] | None:
    """Convert one Upstox full/ltpc feed node → OrderFlowMap market_data.data."""
    block = _market_ff(node) or node
    ltpc = block.get("ltpc") if isinstance(block.get("ltpc"), dict) else node.get("ltpc")
    if not isinstance(ltpc, dict) or ltpc.get("ltp") is None:
        return None
    try:
        ltp = float(ltpc["ltp"])
    except (TypeError, ValueError):
        return None
    if ltp <= 0:
        return None

    ltt = ltpc.get("ltt")
    try:
        ltt_ms = int(ltt) if ltt is not None else int(time.time() * 1000)
    except (TypeError, ValueError):
        ltt_ms = int(time.time() * 1000)

    volume = 0
    for vol_key in ("vtt", "volume", "vol"):
        if block.get(vol_key) is not None:
            try:
                volume = int(float(block[vol_key]))
                break
            except (TypeError, ValueError):
                pass

    buy: list[dict[str, Any]] = []
    sell: list[dict[str, Any]] = []
    level = block.get("marketLevel") or block.get("market_level") or {}
    quotes = []
    if isinstance(level, dict):
        quotes = level.get("bidAskQuote") or level.get("bid_ask_quote") or []
    if isinstance(quotes, list):
        for q in quotes:
            if not isinstance(q, dict):
                continue
            try:
                bp = float(q.get("bidP") or q.get("bid_p") or 0)
                bq = int(float(q.get("bidQ") or q.get("bid_q") or 0))
                ap = float(q.get("askP") or q.get("ask_p") or 0)
                aq = int(float(q.get("askQ") or q.get("ask_q") or 0))
            except (TypeError, ValueError):
                continue
            if bp > 0 and bq > 0:
                buy.append({"price": bp, "quantity": bq, "orders": 1})
            if ap > 0 and aq > 0:
                sell.append({"price": ap, "quantity": aq, "orders": 1})

    return {
        "ltp": ltp,
        "volume": volume,
        "ltt": ltt_ms,
        "depth": {"buy": buy, "sell": sell},
    }


def _expiry_ymd(raw: object) -> str:
    """Normalize Upstox expiry (ISO string or unix ms) to YYYY-MM-DD."""
    if raw is None:
        return "9999-99-99"
    if isinstance(raw, (int, float)):
        # Upstox master often stores expiry as unix ms
        try:
            from datetime import datetime, timezone

            ts = float(raw)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return "9999-99-99"
    text = str(raw).strip()
    if text.isdigit():
        return _expiry_ymd(int(text))
    return text[:10] if text else "9999-99-99"


def _pick_nearest_future(rows: list[dict], code: str) -> str | None:
    today = date.today().isoformat()
    candidates: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("segment") or "") not in ("NSE_FO", "BSE_FO"):
            continue
        inst = str(row.get("instrument_type") or "").upper()
        if inst in ("CE", "PE", "OPTIDX", "OPTSTK"):
            continue
        # Prefer name match for index futures (trading_symbol is "NIFTY FUT 28 JUL 26")
        name = str(row.get("name") or "").upper()
        tsym = str(row.get("trading_symbol") or "").upper()
        if name and name != code and not tsym.startswith(code + " "):
            if not (tsym.startswith(code) and "FUT" in tsym):
                continue
        if code == "NIFTY":
            if name in ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"):
                continue
            if any(x in tsym for x in ("BANKNIFTY", "FINNIFTY", "MIDCP", "NXT50", "NIFTYNXT")):
                continue
        if name and name != code:
            continue
        if tsym.endswith("CE") or tsym.endswith("PE"):
            continue
        exp = _expiry_ymd(row.get("expiry") if row.get("expiry") is not None else row.get("expiry_date"))
        if exp < today:
            continue
        key = str(row.get("instrument_key") or "")
        if key:
            candidates.append((exp, key))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


class InstrumentResolver:
    def __init__(
        self,
        token: str,
        base_url: str = "https://api.upstox.com/v2",
        forced_keys: dict[str, str] | None = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._cache: dict[str, str] = {}
        self._forced = {k.upper(): v for k, v in (forced_keys or {}).items()}
        self._last_error = ""

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": _BROWSER_UA,
        }

    def resolve(self, symbol: str, exchange: str) -> str | None:
        sym = (symbol or "").strip().upper()
        exch = (exchange or "NFO").strip().upper()
        cache_key = f"{exch}:{sym}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        if "|" in sym:  # already an Upstox instrument key
            self._cache[cache_key] = sym
            return sym

        if sym in self._forced:
            self._cache[cache_key] = self._forced[sym]
            return self._forced[sym]

        index = _INDEX_ALIASES.get(sym)
        if not index and sym.startswith("NIFTY") and "BANK" not in sym and "FIN" not in sym:
            index = "NIFTY"
        if not index and "BANKNIFTY" in sym:
            index = "BANKNIFTY"

        key: str | None = None
        if index:
            key = self._resolve_index_future(index)
        if not key:
            key = self._search_symbol(sym, exch)

        if key:
            self._cache[cache_key] = key
            logger.info("Resolved %s@%s → %s", sym, exch, key)
        else:
            logger.error(
                "Could not resolve %s@%s%s",
                sym,
                exch,
                f" ({self._last_error})" if self._last_error else "",
            )
        return key

    def _resolve_index_future(self, code: str) -> str | None:
        exchange = "BSE" if code == "SENSEX" else "NSE"
        merged: list[dict] = []
        seen: set[str] = set()
        for params in (
            {"query": code, "exchanges": exchange, "segments": "FO", "expiry": "current_month", "records": 30},
            {"query": code, "exchanges": exchange, "segments": "FO", "expiry": "near_month", "records": 30},
            {"query": code, "exchanges": exchange, "segments": "FO", "records": 30},
        ):
            try:
                r = requests.get(
                    f"{self.base_url}/instruments/search",
                    params=params,
                    headers=self._headers(),
                    timeout=20,
                )
                if r.status_code == 401:
                    self._last_error = "Upstox token invalid/expired (HTTP 401)"
                    logger.error(self._last_error)
                    break
                r.raise_for_status()
                data = (r.json() or {}).get("data") or []
            except Exception as exc:
                self._last_error = f"instruments/search: {exc}"
                logger.warning("instruments/search failed: %s", exc)
                continue
            if not isinstance(data, list):
                continue
            for row in data:
                if not isinstance(row, dict):
                    continue
                k = str(row.get("instrument_key") or "")
                if not k or k in seen:
                    continue
                seen.add(k)
                merged.append(row)

        key = _pick_nearest_future(merged, code)
        if key:
            return key
        return self._resolve_from_master(code)

    def _resolve_from_master(self, code: str) -> str | None:
        """Offline/fallback: scan Upstox complete.json.gz (public asset, no token)."""
        import gzip
        import time as _time

        gz = REPO_ROOT / "src" / "server" / "data" / "instruments" / "upstox_complete.json.gz"
        url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
        try:
            stale = (not gz.exists()) or ((_time.time() - gz.stat().st_mtime) > 12 * 3600)
            if stale:
                logger.info("Refreshing Upstox instrument master…")
                resp = requests.get(url, timeout=120, headers={"User-Agent": _BROWSER_UA})
                resp.raise_for_status()
                gz.parent.mkdir(parents=True, exist_ok=True)
                gz.write_bytes(resp.content)
            with gzip.open(gz, "rt", encoding="utf-8") as handle:
                rows = json.load(handle)
        except Exception as exc:
            self._last_error = f"master read failed: {exc}"
            return None
        if not isinstance(rows, list):
            return None
        key = _pick_nearest_future(rows, code)
        if key:
            self._last_error = ""
            logger.info("Resolved %s from instrument master → %s", code, key)
        else:
            self._last_error = self._last_error or f"no live {code} FUT in master"
        return key

    def _search_symbol(self, sym: str, exch: str) -> str | None:
        pair = _EXCHANGE_MAP.get(exch, ("NSE", "FO"))
        try:
            r = requests.get(
                f"{self.base_url}/instruments/search",
                params={"query": sym, "exchanges": pair[0], "segments": pair[1], "records": 20},
                headers=self._headers(),
                timeout=20,
            )
            r.raise_for_status()
            data = (r.json() or {}).get("data") or []
        except Exception as exc:
            logger.warning("symbol search failed: %s", exc)
            return None
        if not isinstance(data, list):
            return None
        for row in data:
            if not isinstance(row, dict):
                continue
            tsym = str(row.get("trading_symbol") or "").upper()
            if tsym == sym or tsym.startswith(sym):
                key = str(row.get("instrument_key") or "")
                if key:
                    return key
        if data and isinstance(data[0], dict):
            return str(data[0].get("instrument_key") or "") or None
        return None


class UpstoxFullFeed:
    """One Upstox V3 streamer in ``full`` mode; fans out OFMap payloads."""

    def __init__(self, token: str) -> None:
        self.token = token
        self._streamer = None
        self._lock = threading.Lock()
        self._want: set[str] = set()
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._started = False

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._boot, name="ofmap-upstox-feed", daemon=True).start()

    def want(self, instrument_key: str) -> None:
        with self._lock:
            is_new = instrument_key not in self._want
            self._want.add(instrument_key)
        if is_new and self._connected:
            self._safe_subscribe([instrument_key])

    def register_queue(self, instrument_key: str, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.setdefault(instrument_key, set()).add(q)
        self.want(instrument_key)

    def unregister_queue(self, instrument_key: str, q: asyncio.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(instrument_key)
            if not subs:
                return
            subs.discard(q)
            if not subs:
                self._subscribers.pop(instrument_key, None)

    def _boot(self) -> None:
        try:
            import upstox_client
        except Exception as exc:
            logger.error("upstox-python-sdk missing: %s", exc)
            return
        try:
            config = upstox_client.Configuration()
            config.access_token = self.token
            api_client = upstox_client.ApiClient(config)
            try:
                api_client.default_headers["User-Agent"] = _BROWSER_UA
            except Exception:
                pass

            with self._lock:
                init_keys = sorted(self._want) or ["NSE_INDEX|Nifty 50"]

            streamer = upstox_client.MarketDataStreamerV3(api_client, init_keys, "full")
            streamer.on("open", self._on_open)
            streamer.on("message", self._on_message)
            streamer.on("error", self._on_error)
            streamer.on("close", self._on_close)
            try:
                streamer.auto_reconnect(True, 5, 20)
            except Exception:
                pass
            self._streamer = streamer
            logger.info("Upstox full-feed connecting (%d key[s])", len(init_keys))
            streamer.connect()
        except Exception as exc:
            logger.exception("Upstox full-feed boot failed: %s", exc)
            self._started = False

    def _on_open(self, *_a) -> None:
        self._connected = True
        with self._lock:
            keys = sorted(self._want)
        if keys:
            self._safe_subscribe(keys)
        logger.info("Upstox full-feed connected; streaming %d key(s)", len(keys))

    def _on_close(self, *_a) -> None:
        self._connected = False
        logger.warning("Upstox full-feed closed")

    def _on_error(self, *args) -> None:
        logger.warning("Upstox full-feed error: %s", args[0] if args else "")

    def _on_message(self, message: object) -> None:
        feeds = _extract_feeds(message)
        if not feeds:
            return
        loop = self._loop
        if loop is None:
            return
        for key, node in feeds.items():
            if not isinstance(node, dict):
                continue
            payload = _to_ofmap_payload(node)
            if not payload:
                continue
            with self._lock:
                queues = list(self._subscribers.get(key, ()))
            for q in queues:
                try:
                    loop.call_soon_threadsafe(self._put_nowait, q, payload)
                except Exception:
                    pass

    @staticmethod
    def _put_nowait(q: asyncio.Queue, payload: dict) -> None:
        try:
            while q.qsize() > 200:
                q.get_nowait()
            q.put_nowait(payload)
        except Exception:
            pass

    def _safe_subscribe(self, keys: list[str]) -> None:
        streamer = self._streamer
        if streamer is None or not keys:
            return
        try:
            streamer.subscribe(keys, "full")
            logger.info("Subscribed full mode: %s", keys)
        except Exception as exc:
            logger.warning("subscribe failed: %s", exc)


async def _client_handler(
    ws: Any,
    feed: UpstoxFullFeed,
    resolver: InstrumentResolver,
    api_key_required: str,
) -> None:
    peer = getattr(ws, "remote_address", None)
    logger.info("Client connected: %s", peer)
    authenticated = False
    instrument_key: str | None = None
    q: asyncio.Queue | None = None
    sender: asyncio.Task | None = None

    async def pump() -> None:
        assert q is not None
        while True:
            payload = await q.get()
            await ws.send(json.dumps({"type": "market_data", "data": payload}))

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps({"type": "error", "message": "invalid json"}))
                continue

            action = str(msg.get("action") or "").lower()

            if action == "authenticate":
                key = str(msg.get("api_key") or "")
                if api_key_required and key != api_key_required:
                    await ws.send(json.dumps({"message": "Authentication failed"}))
                    continue
                authenticated = True
                await ws.send(json.dumps({"message": "Authentication successful"}))
                continue

            if not authenticated:
                await ws.send(json.dumps({"message": "Authentication failed"}))
                continue

            if action == "subscribe":
                symbol = str(msg.get("symbol") or "").strip()
                exchange = str(msg.get("exchange") or "NFO").strip()
                key = resolver.resolve(symbol, exchange)
                if not key:
                    detail = resolver._last_error or "not found"
                    await ws.send(
                        json.dumps(
                            {
                                "type": "subscribe",
                                "status": "error",
                                "message": f"unresolved {symbol}@{exchange}: {detail}",
                            }
                        )
                    )
                    continue

                if q is not None and instrument_key:
                    feed.unregister_queue(instrument_key, q)
                if sender:
                    sender.cancel()

                instrument_key = key
                q = asyncio.Queue(maxsize=256)
                feed.register_queue(key, q)
                feed.start()
                sender = asyncio.create_task(pump())
                await ws.send(
                    json.dumps(
                        {
                            "type": "subscribe",
                            "status": "success",
                            "message": f"{symbol} → {key}",
                        }
                    )
                )
                continue

            if action == "unsubscribe" and instrument_key and q is not None:
                feed.unregister_queue(instrument_key, q)
                instrument_key = None
    except Exception as exc:
        logger.info("Client disconnected (%s): %s", peer, exc)
    finally:
        if sender:
            sender.cancel()
        if instrument_key and q is not None:
            feed.unregister_queue(instrument_key, q)
        logger.info("Client closed: %s", peer)


async def run_server(
    host: str,
    port: int,
    username: str,
    api_key: str,
    forced_keys: dict[str, str] | None = None,
) -> None:
    creds = load_upstox_credentials_for_user(username)
    token = normalize_access_token(creds.get("access_token") or "")
    if not token:
        raise SystemExit(
            f"No Upstox access_token for user {username}. "
            f"Expected src/server/data/users/{username}/upstox_credentials.json"
        )

    base = (creds.get("base_url") or "https://api.upstox.com/v2").rstrip("/")
    resolver = InstrumentResolver(token, base_url=base, forced_keys=forced_keys)
    feed = UpstoxFullFeed(token)
    loop = asyncio.get_running_loop()
    feed.set_loop(loop)

    import websockets

    async def handler(ws: Any) -> None:
        await _client_handler(ws, feed, resolver, api_key)

    logger.info(
        "OrderFlowMap bridge listening on ws://%s:%d  (user=%s, api_key=%s)",
        host,
        port,
        username,
        api_key or "(any)",
    )
    async with websockets.serve(handler, host, port, ping_interval=20, ping_timeout=20):
        await asyncio.Future()


def main() -> None:
    parser = argparse.ArgumentParser(description="Upstox → OrderFlowMap Live bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--user", default="AK07", help="Credentials user bucket")
    parser.add_argument(
        "--api-key",
        default="ak07",
        help="Key OrderFlowMap must send (empty = accept any)",
    )
    parser.add_argument(
        "--nifty-key",
        default="",
        help="Force Upstox instrument_key for symbol NIFTY (e.g. NSE_FO|12345)",
    )
    parser.add_argument(
        "--banknifty-key",
        default="",
        help="Force Upstox instrument_key for symbol BANKNIFTY",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    forced: dict[str, str] = {}
    if args.nifty_key:
        forced["NIFTY"] = args.nifty_key.strip()
        forced["NIFTY-I"] = args.nifty_key.strip()
    if args.banknifty_key:
        forced["BANKNIFTY"] = args.banknifty_key.strip()
        forced["BANKNIFTY-I"] = args.banknifty_key.strip()
        forced["BN"] = args.banknifty_key.strip()
    try:
        asyncio.run(
            run_server(args.host, args.port, args.user, args.api_key, forced_keys=forced)
        )
    except KeyboardInterrupt:
        logger.info("Stopped")


if __name__ == "__main__":
    main()
