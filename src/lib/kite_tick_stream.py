"""Zerodha Kite WebSocket tick stream (LTP mode) — thread-safe last-price cache."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

try:
    from kiteconnect import KiteTicker
except ImportError:
    KiteTicker = None  # type: ignore[misc, assignment]


class KiteTickStream:
    """
    Subscribes to instrument tokens in MODE_LTP; updates last_price by script name.
    """

    def __init__(
        self,
        api_key: str,
        access_token: str,
        log: logging.Logger | None = None,
    ):
        self._api_key = (api_key or "").strip()
        self._access_token = (access_token or "").strip()
        self._log = log or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._ltp_by_script: dict[str, float] = {}
        self._token_to_script: dict[int, str] = {}
        self._scripts: set[str] = set()
        self._kws: Any = None
        self._started = False
        # Invoked on the KiteTicker thread after each tick batch (LTP cache updated). Optional.
        self.on_after_ticks: Optional[Callable[[], None]] = None

    def set_subscriptions(self, script_to_token: dict[str, int]) -> None:
        """Replace subscription map (script → Kite instrument_token)."""
        with self._lock:
            self._token_to_script = {}
            self._ltp_by_script = {}
            self._scripts = set()
            for script, tok in (script_to_token or {}).items():
                s = str(script or "").strip().upper()
                try:
                    t = int(tok)
                except (TypeError, ValueError):
                    continue
                if not s or t <= 0:
                    continue
                self._token_to_script[t] = s
                self._scripts.add(s)
        if self._started and self._kws is not None and KiteTicker is not None:
            tokens = list(self._token_to_script.keys())
            if tokens:
                try:
                    ws_obj = getattr(self._kws, "ws", None)
                    if ws_obj is None:
                        # Socket not ready yet (or reconnecting). on_connect will subscribe.
                        return
                    self._kws.subscribe(tokens)
                    self._kws.set_mode(KiteTicker.MODE_LTP, tokens)
                    self._log.info("Kite ticker: resubscribed count=%d", len(tokens))
                except Exception as e:
                    txt = str(e)
                    if "sendMessage" in txt and "NoneType" in txt:
                        self._log.info("Kite ticker: resubscribe deferred (socket reconnecting)")
                    else:
                        self._log.warning("Kite ticker resubscribe failed: %s", e)

    def last_price(self, script_name: str) -> float | None:
        s = str(script_name or "").strip().upper()
        with self._lock:
            v = self._ltp_by_script.get(s)
            return float(v) if v is not None and v > 0 else None

    def _on_ticks(self, ws, ticks: list[dict[str, Any]]) -> None:
        if not isinstance(ticks, list):
            return
        with self._lock:
            for t in ticks:
                if not isinstance(t, dict):
                    continue
                try:
                    tok = int(t.get("instrument_token") or 0)
                except (TypeError, ValueError):
                    continue
                script = self._token_to_script.get(tok)
                if not script:
                    continue
                lp = t.get("last_price")
                if lp is None:
                    lp = t.get("last_trade_price")
                try:
                    lp_f = float(lp)
                except (TypeError, ValueError):
                    continue
                if lp_f > 0:
                    self._ltp_by_script[script] = lp_f
        fn = self.on_after_ticks
        if fn is not None:
            try:
                fn()
            except Exception:
                self._log.exception("on_after_ticks callback failed")

    def _on_connect(self, ws, response: Any) -> None:
        tokens = list(self._token_to_script.keys())
        if not tokens or KiteTicker is None:
            return
        try:
            ws.subscribe(tokens)
            ws.set_mode(KiteTicker.MODE_LTP, tokens)
            self._log.info("Kite ticker: subscribed MODE_LTP count=%d", len(tokens))
        except Exception as e:
            self._log.error("Kite ticker subscribe failed: %s", e)

    def _on_close(self, ws, code: Any, reason: Any) -> None:
        self._log.warning("Kite ticker closed: code=%s reason=%s", code, reason)

    def _on_error(self, ws, code: Any, reason: Any) -> None:
        self._log.warning("Kite ticker error: code=%s reason=%s", code, reason)

    def start(self) -> bool:
        if KiteTicker is None:
            self._log.error("kiteconnect package not installed; pip install kiteconnect")
            return False
        if not self._api_key or not self._access_token:
            self._log.error("Kite ticker: missing api_key or access_token")
            return False
        if self._started:
            return True
        if not self._token_to_script:
            self._log.warning("Kite ticker: no tokens to subscribe")
            return False
        self._kws = KiteTicker(self._api_key, self._access_token)
        self._kws.on_ticks = self._on_ticks
        self._kws.on_connect = self._on_connect
        self._kws.on_close = self._on_close
        self._kws.on_error = self._on_error
        self._kws.connect(threaded=True)
        self._started = True
        self._log.info("Kite ticker: connect() started (threaded)")
        return True

    def stop(self) -> None:
        if self._kws is not None:
            try:
                self._kws.close()
            except Exception:
                pass
        self._kws = None
        self._started = False
