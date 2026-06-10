"""AK07 Context Bridge - MCP server exposing the live trading state to a local LLM.

This is the asymmetric observer side of the AK07 architecture:

- READ side : the LLM inspects the live market snapshot published to Redis by
  the main trading loop (Upstox V3 WebSocket -> Volume Order Block engine).
- WRITE side: the LLM publishes a single, narrow directive (the system bias)
  back to Redis. It never touches orders, positions, or execution directly.

Run standalone (background-friendly):

    python mcp_server.py                          # streamable HTTP on 127.0.0.1:8765
    python mcp_server.py --transport stdio        # stdio for direct client spawning
    python mcp_server.py --host 0.0.0.0 --port 9000

The HTTP endpoint is served at /mcp (e.g. http://127.0.0.1:8765/mcp).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running this file directly from anywhere (`python src/server/src/mcp_server.py`).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP

from app.services import cache_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ak07.mcp_server")

mcp = FastMCP("AK07_Context_Engine")


@mcp.tool()
def read_live_market_context() -> str:
    """Read the live AK07 market snapshot (spot, option-chain walls, freshness).

    WHEN TO USE:
    Call this tool FIRST, before forming any market opinion or before calling
    `update_algorithmic_bias`. Use it to inspect the current option chain setup
    when validating a structural breakout, a Volume Order Block retest, or any
    directional thesis. Always re-read it if more than a minute has passed,
    since the snapshot expires after 300 seconds.

    WHY IT MATTERS:
    The summary tells you where the Call Wall (highest call OI strike, acts as
    overhead resistance) and the Put Floor (highest put OI strike, acts as
    support) sit relative to spot. A breakout signal into a nearby Call Wall is
    structurally weak; a long setup just above the Put Floor is structurally
    supported. The timestamp tells you exactly how fresh the data is - never
    reason from stale data.

    Returns:
        A plain-text summary of spot price, traded volume, distance to the
        Call Wall and Put Floor (in points and percent), and the snapshot
        timestamp. If no live snapshot is available (market closed, feed down,
        or cache expired), returns an explicit message saying so - in that
        case, do NOT guess market conditions and do NOT update the bias.
    """
    snapshot = cache_manager.get_market_snapshot()
    if snapshot is None:
        return (
            "NO LIVE MARKET DATA AVAILABLE. The 'ak07:live_state' cache is empty, "
            "expired (TTL 300s), or Redis is unreachable. Do not infer market "
            "conditions and do not change the algorithmic bias."
        )

    try:
        spot = float(snapshot["spot_price"])
        volume = int(snapshot["volume"])
        call_wall = int(snapshot["highest_call_oi_strike"])
        put_floor = int(snapshot["highest_put_oi_strike"])
        timestamp = str(snapshot["timestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("Malformed live_state payload: %s (%s)", snapshot, exc)
        return (
            "LIVE MARKET DATA IS MALFORMED and cannot be summarized safely. "
            "Do not act on it and do not change the algorithmic bias."
        )

    call_dist = call_wall - spot
    put_dist = spot - put_floor
    call_pct = (call_dist / spot * 100) if spot else 0.0
    put_pct = (put_dist / spot * 100) if spot else 0.0

    call_side = "above" if call_dist >= 0 else "BELOW (spot has breached the Call Wall)"
    put_side = "below" if put_dist >= 0 else "ABOVE (spot has broken under the Put Floor)"

    summary = (
        f"AK07 LIVE MARKET CONTEXT (data timestamp: {timestamp})\n"
        f"- Spot Price: {spot:.2f} | Session Volume: {volume:,}\n"
        f"- Call Wall (highest Call OI, resistance): {call_wall} -> "
        f"{abs(call_dist):.2f} pts ({abs(call_pct):.2f}%) {call_side} spot\n"
        f"- Put Floor (highest Put OI, support): {put_floor} -> "
        f"{abs(put_dist):.2f} pts ({abs(put_pct):.2f}%) {put_side} spot\n"
        f"- Tradeable range width (Call Wall - Put Floor): {call_wall - put_floor} pts"
    )
    logger.info("Served live market context (spot=%.2f, ts=%s)", spot, timestamp)
    return summary


@mcp.tool()
def update_algorithmic_bias(bias_mode: str) -> str:
    """Set the directional bias filter of the AK07 execution engine.

    WHEN TO USE:
    Call this ONLY after you have called `read_live_market_context` in the same
    reasoning step and formed a clear structural view. Use it when the option
    chain geometry justifies constraining trade direction:
    - "LONG_ONLY"  -> spot is holding well above the Put Floor with room below
      the Call Wall; suppress short entries.
    - "SHORT_ONLY" -> spot is rejecting at or breaking down from the Call Wall;
      suppress long entries.
    - "NEUTRAL"    -> structure is balanced, data is stale/unavailable, or you
      are uncertain. NEUTRAL is the safe default; prefer it whenever in doubt.

    WHY IT MATTERS:
    This is your ONLY write channel into the trading system. It does not place
    or cancel orders; it only gates which side of new Volume Order Block
    signals the execution engine is allowed to take. A wrong bias silently
    filters out valid trades or exposes the book to one-sided risk, so change
    it deliberately and infrequently, not on every minor fluctuation.

    Args:
        bias_mode: Exactly one of "LONG_ONLY", "SHORT_ONLY", or "NEUTRAL"
            (case-insensitive; anything else is rejected).

    Returns:
        A confirmation string with the applied bias, or an error string
        explaining why the update was rejected. If rejected, the previous
        bias remains in force.
    """
    requested = str(bias_mode).strip().upper()
    if requested not in cache_manager.VALID_BIASES:
        return (
            f"REJECTED: '{bias_mode}' is not a valid bias. The system bias is "
            f"unchanged. Valid values: LONG_ONLY, SHORT_ONLY, NEUTRAL."
        )

    if not cache_manager.set_system_bias(requested):
        return (
            "FAILED: Redis is unreachable, so the bias could not be updated. "
            "The execution engine keeps its previous bias. Try again later."
        )

    logger.info("Algorithmic bias updated via MCP: %s", requested)
    return f"CONFIRMED: AK07 system bias is now '{requested}'. The execution engine will apply it immediately."


def main() -> None:
    parser = argparse.ArgumentParser(description="AK07 Context Engine MCP server")
    parser.add_argument(
        "--transport",
        choices=("streamable-http", "sse", "stdio"),
        default="streamable-http",
        help="MCP transport (default: streamable-http, suited for a background server process)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for HTTP transports")
    parser.add_argument("--port", type=int, default=8765, help="Bind port for HTTP transports")
    args = parser.parse_args()

    mcp.settings.host = args.host
    mcp.settings.port = args.port

    if args.transport == "stdio":
        logger.info("Starting AK07_Context_Engine on stdio transport")
    else:
        logger.info(
            "Starting AK07_Context_Engine on %s transport at http://%s:%d/%s",
            args.transport,
            args.host,
            args.port,
            "mcp" if args.transport == "streamable-http" else "sse",
        )
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
