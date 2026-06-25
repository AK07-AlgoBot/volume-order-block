"""AK07 Telegram alert system (non-blocking Markdown + photo notifier).

Delivery runs on a dedicated daemon worker thread fed by a queue, so the
trading engine's hot path only pays for a queue put (microseconds). Network
errors, Telegram rate limits (HTTP 429), and bad configuration are absorbed
and logged — a notification failure can never lag or crash the engine.

Configuration (environment):
    TELEGRAM_BOT_TOKEN  Bot token from @BotFather.
    TELEGRAM_CHAT_ID    Target chat/channel id.

Both are read from the environment (a repo/server `.env` is loaded by
`app.config.paths` at import time elsewhere in the app).
"""

from __future__ import annotations

import io
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Final

import httpx

logger = logging.getLogger("ak07.telegram_notifier")

TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"
SEND_TIMEOUT_SECONDS: Final[float] = 10.0
PHOTO_TIMEOUT_SECONDS: Final[float] = 20.0
MAX_QUEUE_SIZE: Final[int] = 200
MAX_SEND_ATTEMPTS: Final[int] = 3
MAX_RATE_LIMIT_WAIT_SECONDS: Final[float] = 30.0


# ---------------------------------------------------------------------------
# Queue message types
# ---------------------------------------------------------------------------

@dataclass
class _TextMsg:
    text: str


@dataclass
class _PhotoMsg:
    image_bytes: bytes
    caption: str


_queue: "queue.Queue[_TextMsg | _PhotoMsg]" = queue.Queue(maxsize=MAX_QUEUE_SIZE)
_worker_started = threading.Event()
_worker_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _credentials() -> tuple[str, str] | None:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return None
    return token, chat_id


# ---------------------------------------------------------------------------
# Delivery helpers
# ---------------------------------------------------------------------------

def _deliver_text(client: httpx.Client, text: str) -> None:
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
                logger.info("Telegram text delivered (%d chars)", len(text))
                return
            if response.status_code == 429:
                wait = min(
                    float((response.json() or {}).get("parameters", {}).get("retry_after", 1)),
                    MAX_RATE_LIMIT_WAIT_SECONDS,
                )
                logger.warning("Telegram rate limit; backing off %.1fs", wait)
                time.sleep(wait)
                continue
            logger.error("Telegram API error: HTTP %d %s", response.status_code, response.text[:300])
            return
        except httpx.HTTPError as exc:
            logger.warning("Telegram network error (attempt %d/%d): %s", attempt, MAX_SEND_ATTEMPTS, exc)
            time.sleep(min(2.0 * attempt, 5.0))
    logger.error("Telegram text alert dropped after %d attempts", MAX_SEND_ATTEMPTS)


def _deliver_photo(client: httpx.Client, image_bytes: bytes, caption: str) -> None:
    creds = _credentials()
    if creds is None:
        return
    token, chat_id = creds
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendPhoto"
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            response = client.post(
                url,
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"},
                files={"photo": ("chart.png", image_bytes, "image/png")},
                timeout=PHOTO_TIMEOUT_SECONDS,
            )
            if response.status_code == 200:
                logger.info("Telegram photo delivered (%d bytes)", len(image_bytes))
                return
            if response.status_code == 429:
                wait = min(
                    float((response.json() or {}).get("parameters", {}).get("retry_after", 1)),
                    MAX_RATE_LIMIT_WAIT_SECONDS,
                )
                time.sleep(wait)
                continue
            logger.error("Telegram sendPhoto error: HTTP %d %s", response.status_code, response.text[:300])
            return
        except httpx.HTTPError as exc:
            logger.warning("Telegram sendPhoto network error (attempt %d): %s", attempt, exc)
            time.sleep(min(2.0 * attempt, 5.0))
    logger.error("Telegram photo alert dropped after %d attempts", MAX_SEND_ATTEMPTS)


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

def _worker_loop() -> None:
    with httpx.Client() as client:
        while True:
            msg = _queue.get()
            try:
                if isinstance(msg, _PhotoMsg):
                    _deliver_photo(client, msg.image_bytes, msg.caption)
                else:
                    _deliver_text(client, msg.text)
            except Exception:  # noqa: BLE001
                logger.exception("Unexpected notifier failure; alert dropped")
            finally:
                _queue.task_done()


def _ensure_worker() -> None:
    if _worker_started.is_set():
        return
    with _worker_lock:
        if _worker_started.is_set():
            return
        t = threading.Thread(target=_worker_loop, name="ak07-telegram-notifier", daemon=True)
        t.start()
        _worker_started.set()
        logger.info("Telegram notifier worker started")


def _enqueue(msg: _TextMsg | _PhotoMsg) -> bool:
    try:
        _ensure_worker()
        _queue.put_nowait(msg)
        return True
    except queue.Full:
        logger.error("Telegram queue full; alert dropped")
        return False
    except Exception:  # noqa: BLE001
        logger.exception("Failed to queue Telegram alert")
        return False


# ---------------------------------------------------------------------------
# Chart generation
# ---------------------------------------------------------------------------

def _generate_trade_chart(
    candles: list[dict],
    entry: float,
    sl: float,
    tp1: float,
    tp2: float | None,
    direction: str,
    index_name: str,
) -> bytes | None:
    """Render a dark-theme candlestick chart with SL/TP zones. Returns PNG bytes."""
    try:
        import matplotlib  # noqa: PLC0415
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: PLC0415
        import matplotlib.ticker as mticker  # noqa: PLC0415

        bars = candles[-15:]
        n = len(bars)

        fig, ax = plt.subplots(figsize=(8, 5))
        fig.patch.set_facecolor("#0c0f14")
        ax.set_facecolor("#161b24")

        # Draw candlesticks
        for i, c in enumerate(bars):
            o = float(c["open"])
            h = float(c["high"])
            lo = float(c["low"])
            cl = float(c["close"])
            color = "#26a69a" if cl >= o else "#ef5350"
            ax.plot([i, i], [lo, h], color=color, linewidth=0.9, zorder=2)
            ax.bar(i, max(abs(cl - o), 0.5), bottom=min(o, cl), color=color, width=0.6, zorder=3)

        # Shaded zones
        sl_lo, sl_hi = sorted([entry, sl])
        tp1_lo, tp1_hi = sorted([entry, tp1])
        ax.axhspan(sl_lo, sl_hi, alpha=0.22, color="#ef5350", zorder=1)
        ax.axhspan(tp1_lo, tp1_hi, alpha=0.22, color="#26a69a", zorder=1)
        if tp2 is not None:
            tp2_lo, tp2_hi = sorted([entry, tp2])
            ax.axhspan(tp2_lo, tp2_hi, alpha=0.10, color="#26a69a", zorder=1)

        # Horizontal lines
        ax.axhline(entry, color="#ffffff", linewidth=1.5, linestyle="--", zorder=4)
        ax.axhline(sl, color="#ef5350", linewidth=1.3, linestyle="-", zorder=4)
        ax.axhline(tp1, color="#26a69a", linewidth=1.3, linestyle="-", zorder=4)
        if tp2 is not None:
            ax.axhline(tp2, color="#66bb6a", linewidth=1.0, linestyle=":", zorder=4)

        # Right-side labels
        lx = n + 0.3
        ax.text(lx, entry, f"  Entry {entry:.0f}", color="#ffffff", va="center", fontsize=8, fontweight="bold")
        ax.text(lx, sl, f"  SL {sl:.0f}", color="#ef5350", va="center", fontsize=8)
        ax.text(lx, tp1, f"  TP1 {tp1:.0f}", color="#26a69a", va="center", fontsize=8)
        if tp2 is not None:
            ax.text(lx, tp2, f"  TP2 {tp2:.0f}", color="#66bb6a", va="center", fontsize=8)

        # Title
        dir_color = "#26a69a" if direction == "LONG" else "#ef5350"
        dir_label = "▲ LONG" if direction == "LONG" else "▼ SHORT"
        ax.set_title(f"{index_name}  {dir_label}", color=dir_color, fontsize=11, fontweight="bold", pad=8)

        # Styling
        ax.set_xlim(-0.5, n + 4.5)
        ax.tick_params(colors="#8899aa", labelsize=7)
        ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.0f"))
        for spine in ax.spines.values():
            spine.set_edgecolor("#232b38")
        ax.grid(axis="y", color="#232b38", linewidth=0.5, zorder=0)
        ax.set_xticks([])

        plt.tight_layout(pad=1.5)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Trade chart generation failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _escape_markdown(text: str) -> str:
    """Escape dynamic text for Telegram legacy Markdown parse_mode."""
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def send_message(text: str) -> bool:
    """Queue a raw Markdown message for async delivery (never blocks)."""
    return _enqueue(_TextMsg(text=text))


def notify_trade_signal_instruction(
    index_name: str,
    trade_type: str,
    entry_price: float,
    target_price: float,
    sl_price: float,
    note: str,
    timestamp: str,
    *,
    strategy: str = "",
    candles: list[dict] | None = None,
) -> bool:
    """Signal-only alert when daily target hit — instructions for manual / other traders."""
    strat = f" ({_escape_markdown(strategy)})" if strategy else ""
    text = (
        "\U0001f4cb *AK07 SIGNAL ONLY* \U0001f4cb\n"
        "_Daily target hit — not sent to Upstox_\n"
        f"\u2022 *Strategy:* {_escape_markdown(index_name)}{strat}\n"
        f"\u2022 *Type:* {_escape_markdown(trade_type)}\n"
        f"\u2022 *Entry:* {entry_price:.2f}\n"
        f"\u2022 *Target:* {target_price:.2f}\n"
        f"\u2022 *Stop-Loss:* {sl_price:.2f}\n"
        f"\u2022 *Note:* {_escape_markdown(note)}\n"
        f"\u2022 *Time:* {_escape_markdown(timestamp)}"
    )
    if candles:
        img = _generate_trade_chart(
            candles=candles,
            entry=entry_price,
            sl=sl_price,
            tp1=target_price,
            tp2=None,
            direction=trade_type,
            index_name=index_name,
        )
        if img:
            return _enqueue(_PhotoMsg(image_bytes=img, caption=text))
    return _enqueue(_TextMsg(text=text))


def notify_position_followup(
    index_name: str,
    message: str,
    timestamp: str,
) -> bool:
    """Follow-on instruction for an open bot trade (SL trail, hold, exit hint)."""
    text = (
        "\U0001f4cc *AK07 TRADE FOLLOW-UP* \U0001f4cc\n"
        f"\u2022 *Index:* {_escape_markdown(index_name)}\n"
        f"\u2022 *Update:* {_escape_markdown(message)}\n"
        f"\u2022 *Time:* {_escape_markdown(timestamp)}"
    )
    return _enqueue(_TextMsg(text=text))


def notify_trade_execution(
    index_name: str,
    trade_type: str,
    entry_price: float,
    target_price: float,
    sl_price: float,
    component_sentiment: str,
    timestamp: str,
    tp2_price: float | None = None,
    candles: list[dict] | None = None,
) -> bool:
    """Dispatch a trade execution alert with an optional SL/TP chart image."""
    if tp2_price is not None:
        target_block = (
            f"\u2022 *TP1 (1R \u2014 book):* {target_price:.2f}\n"
            f"\u2022 *TP2 (2R):* {tp2_price:.2f}\n"
            f"\u2022 *R:R:* 1:2\n"
        )
    else:
        target_block = f"\u2022 *Target:* {target_price:.2f}\n"

    text = (
        "\U0001f6a8 *AK07 TRADE EXECUTION* \U0001f6a8\n"
        f"\u2022 *Index:* {_escape_markdown(index_name)}\n"
        f"\u2022 *Type:* {_escape_markdown(trade_type)}\n"
        f"\u2022 *Entry:* {entry_price:.2f}\n"
        f"{target_block}"
        f"\u2022 *Stop-Loss:* {sl_price:.2f}\n"
        f"\u2022 *Sentiment:* {_escape_markdown(component_sentiment)}\n"
        f"\u2022 *Time:* {_escape_markdown(timestamp)}"
    )

    if candles:
        img = _generate_trade_chart(
            candles=candles,
            entry=entry_price,
            sl=sl_price,
            tp1=target_price,
            tp2=tp2_price,
            direction=trade_type,
            index_name=index_name,
        )
        if img:
            return _enqueue(_PhotoMsg(image_bytes=img, caption=text))

    return _enqueue(_TextMsg(text=text))


def notify_trade_exit(
    index_name: str,
    trade_type: str,
    exit_price: float,
    pnl_points: float,
    reason: str,
    timestamp: str,
) -> bool:
    """Dispatch a trade exit / square-off alert."""
    emoji = "\u2705" if pnl_points >= 0 else "\u274c"
    text = (
        f"{emoji} *AK07 TRADE CLOSED* {emoji}\n"
        f"\u2022 *Index:* {_escape_markdown(index_name)}\n"
        f"\u2022 *Type:* {_escape_markdown(trade_type)}\n"
        f"\u2022 *Exit:* {exit_price:.2f}\n"
        f"\u2022 *P&L:* {pnl_points:+.2f} pts\n"
        f"\u2022 *Reason:* {_escape_markdown(reason)}\n"
        f"\u2022 *Time:* {_escape_markdown(timestamp)}"
    )
    return _enqueue(_TextMsg(text=text))


def notify_system_event(title: str, detail: str) -> bool:
    """Dispatch a system-level alert (kill switch, time gate, engine errors)."""
    text = (
        f"\u26a0\ufe0f *AK07 SYSTEM EVENT: {_escape_markdown(title)}*\n"
        f"{_escape_markdown(detail)}"
    )
    return _enqueue(_TextMsg(text=text))
