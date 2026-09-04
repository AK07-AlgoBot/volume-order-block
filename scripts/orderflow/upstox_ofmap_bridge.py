#!/usr/bin/env python3
"""CLI wrapper — prefer hosting via FastAPI: https://ak07.in/ofmap/

  python scripts/orderflow/upstox_ofmap_bridge.py --port 8766
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src" / "server" / "src"))
sys.path.insert(0, str(REPO / "src" / "lib"))

from app.services.ofmap_bridge import (  # noqa: E402
    InstrumentResolver,
    UpstoxFullFeed,
    handle_ofmap_client,
    ofmap_api_key_required,
)
from upstox_credentials_store import (  # noqa: E402
    load_upstox_credentials_for_user,
    normalize_access_token,
)

logger = logging.getLogger("ak07.ofmap_bridge")


class _WsAdapter:
    """Adapt websockets library connection to Starlette-like send_text/receive_text."""

    def __init__(self, ws):
        self._ws = ws
        self.client = getattr(ws, "remote_address", None)

    async def send_text(self, data: str) -> None:
        await self._ws.send(data)

    async def receive_text(self) -> str:
        return await self._ws.recv()


async def run_server(host: str, port: int, username: str, api_key: str) -> None:
    creds = load_upstox_credentials_for_user(username)
    token = normalize_access_token(creds.get("access_token") or "")
    if not token:
        raise SystemExit(f"No Upstox token for {username}")
    base = (creds.get("base_url") or "https://api.upstox.com/v2").rstrip("/")
    feed = UpstoxFullFeed.shared(token)
    feed.set_loop(asyncio.get_running_loop())
    resolver = InstrumentResolver(token, base_url=base)

    import websockets

    async def handler(ws):
        await handle_ofmap_client(
            _WsAdapter(ws),
            feed=feed,
            resolver=resolver,
            api_key_required=api_key,
        )

    logger.info("Local bridge ws://%s:%d (prefer https://ak07.in/ofmap/)", host, port)
    async with websockets.serve(handler, host, port, ping_interval=20, ping_timeout=20):
        await asyncio.Future()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--user", default="AK07")
    p.add_argument("--api-key", default="ak07")
    p.add_argument("-v", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.v else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(run_server(args.host, args.port, args.user, args.api_key))
    except KeyboardInterrupt:
        logger.info("Stopped")


if __name__ == "__main__":
    main()
