"""AK07 Telegram alert system (non-blocking Markdown notifier).

Delivery runs on a dedicated daemon worker thread fed by a queue, so the
trading engine's hot path only pays for a queue put (microseconds). Network
errors, Telegram rate limits (HTTP 429), and bad configuration are absorbed
and logged - a notification failure can never lag or crash the engine.

Configuration (environment):
    TELEGRAM_BOT_TOKEN  Bot token from @BotFather.
    TELEGRAM_CHAT_ID    Target chat/channel id.

Both are read from the environment (a repo/server `.env` is loaded by
`app.config.paths` at import time elsewhere in the app).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Final

import httpx

logger = logging.getLogger("ak07.telegram_notifier")

TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"
SEND_TIMEOUT_SECONDS: Final[float] = 10.0
MAX_QUEUE_SIZE: Final[int] = 200
MAX_SEND_ATTEMPTS: Final[int] = 3
MAX_RATE_LIMIT_WAIT_SECONDS: Final[float] = 30.0

_queue: "queue.Queue[str]" = queue.Queue(maxsize=MAX_QUEUE_SIZE)
_worker_started = threading.Event()
_worker_lock = threading.Lock()


def _credentials() -> tuple[str, str] | None:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return None
    return token, chat_id


def _deliver(client: httpx.Client, text: str) -> None:
    """Send one message with bounded retries; absorbs every failure."""
    creds = _credentials()
    if creds is None:
        logger.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; alert dropped")
        return
    token, chat_id = creds
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            response = client.post(url, json=payload, timeout=SEND_TIMEOUT_SECONDS)
            if response.status_code == 200:
                logger.info("Telegram alert delivered (%d chars)", len(text))
                return
            if response.status_code == 429:
                retry_after = 1.0
                try:
                    retry_after = float(response.json().get("parameters", {}).get("retry_after", 1))
                except Exception:  # noqa: BLE001
                    pass
                wait = min(retry_after, MAX_RATE_LIMIT_WAIT_SECONDS)
                logger.warning("Telegram rate limit hit; backing off %.1fs (attempt %d)", wait, attempt)
                time.sleep(wait)
                continue
            logger.error(
                "Telegram API rejected message: HTTP %d %s",
                response.status_code,
                (response.text or "")[:300],
            )
            return
        except httpx.HTTPError as exc:
            logger.warning("Telegram network error (attempt %d/%d): %s", attempt, MAX_SEND_ATTEMPTS, exc)
            time.sleep(min(2.0 * attempt, 5.0))
    logger.error("Telegram alert dropped after %d attempts", MAX_SEND_ATTEMPTS)


def _worker_loop() -> None:
    with httpx.Client() as client:
        while True:
            text = _queue.get()
            try:
                _deliver(client, text)
            except Exception:  # noqa: BLE001 - the worker must never die
                logger.exception("Unexpected notifier failure; alert dropped")
            finally:
                _queue.task_done()


def _ensure_worker() -> None:
    if _worker_started.is_set():
        return
    with _worker_lock:
        if _worker_started.is_set():
            return
        thread = threading.Thread(target=_worker_loop, name="ak07-telegram-notifier", daemon=True)
        thread.start()
        _worker_started.set()
        logger.info("Telegram notifier worker started")


def send_message(text: str) -> bool:
    """Queue a raw Markdown message for asynchronous delivery (never blocks).

    Returns True if queued, False if the queue is full or notifier unusable.
    """
    try:
        _ensure_worker()
        _queue.put_nowait(text)
        return True
    except queue.Full:
        logger.error("Telegram queue full (%d pending); alert dropped", MAX_QUEUE_SIZE)
        return False
    except Exception:  # noqa: BLE001
        logger.exception("Failed to queue Telegram alert")
        return False


def notify_trade_execution(
    index_name: str,
    trade_type: str,
    entry_price: float,
    target_price: float,
    sl_price: float,
    component_sentiment: str,
    timestamp: str,
) -> bool:
    """Dispatch the formatted AK07 trade execution alert (fire-and-forget)."""
    text = (
        "\U0001f6a8 **AK07 TRADE EXECUTION ALERT** \U0001f6a8\n"
        f"\u2022 **Index:** {index_name}\n"
        f"\u2022 **Type:** {trade_type}\n"
        f"\u2022 **Entry Price:** {entry_price:.2f}\n"
        f"\u2022 **Target:** {target_price:.2f}\n"
        f"\u2022 **Stop-Loss:** {sl_price:.2f}\n"
        f"\u2022 **Component Sentiment:** {component_sentiment}\n"
        f"\u2022 **Execution Time:** {timestamp}"
    )
    return send_message(text)


def notify_trade_exit(
    index_name: str,
    trade_type: str,
    exit_price: float,
    pnl_points: float,
    reason: str,
    timestamp: str,
) -> bool:
    """Dispatch a trade exit / square-off alert (fire-and-forget)."""
    emoji = "\u2705" if pnl_points >= 0 else "\u274c"
    text = (
        f"{emoji} **AK07 TRADE CLOSED** {emoji}\n"
        f"\u2022 **Index:** {index_name}\n"
        f"\u2022 **Type:** {trade_type}\n"
        f"\u2022 **Exit Price:** {exit_price:.2f}\n"
        f"\u2022 **P&L:** {pnl_points:+.2f} pts\n"
        f"\u2022 **Reason:** {reason}\n"
        f"\u2022 **Exit Time:** {timestamp}"
    )
    return send_message(text)


def notify_system_event(title: str, detail: str) -> bool:
    """Dispatch a system-level alert (kill switch, time gate, engine errors)."""
    text = f"\u26a0\ufe0f **AK07 SYSTEM EVENT: {title}**\n{detail}"
    return send_message(text)
