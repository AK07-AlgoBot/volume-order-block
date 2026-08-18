"""Strategy 29 — Nifty ORB+ paper (S7 v7 gates, Nifty only, no live orders)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.constants import S29_ORB_INDICES
from app.services import cache_manager, performance_store
from app.services.s7_vwap_breakout_engine import S7Engine

logger = logging.getLogger("ak07.s29_nifty_orb")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    engine = S7Engine(
        index_codes=S29_ORB_INDICES,
        paper_trading=True,
        strategy_label=performance_store.STRATEGY_S29_ORB,
        strategy_id="s29_orb",
        cache_key=cache_manager.S29_STATE_KEY,
        short_name="S29",
    )
    logger.info("S29 Nifty ORB+ paper engine — no live orders")
    engine.run()


if __name__ == "__main__":
    main()
