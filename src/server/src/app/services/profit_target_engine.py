"""Poll Upstox portfolio P&L and enforce daily profit targets."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as dtime
from typing import Final
from zoneinfo import ZoneInfo

from app.services.daily_profit_guard import (
    PROFIT_GUARD_ENABLED,
    engage_daily_profit_target,
    effective_target_inr,
    is_expiry_trading_day,
    load_state,
    publish_upstox_pnl_snapshot,
    profit_target_engaged,
    save_state,
    should_engage_target,
)
from app.services.upstox_engine import MOCK_MODE, PAPER_TRADING, build_upstox_client

logger = logging.getLogger("ak07.profit_target_engine")

IST: Final = ZoneInfo("Asia/Kolkata")
POLL_SECONDS: Final[int] = int(os.environ.get("AK07_PROFIT_GUARD_POLL_SEC", "5"))
SESSION_START: Final[dtime] = dtime(9, 0)
SESSION_END: Final[dtime] = dtime(15, 45)


class ProfitTargetEngine:
    def __init__(self) -> None:
        self._client = None if (MOCK_MODE or PAPER_TRADING) else build_upstox_client()
        logger.info(
            "Profit target engine started | guard=%s | paper=%s",
            PROFIT_GUARD_ENABLED,
            PAPER_TRADING,
        )

    def run(self) -> None:
        while True:
            now = datetime.now(IST)
            t = now.time()
            if SESSION_START <= t <= SESSION_END:
                try:
                    self._tick(now)
                except Exception:
                    logger.exception("Profit target tick failed")
            time.sleep(POLL_SECONDS)

    def _tick(self, now: datetime) -> None:
        if profit_target_engaged():
            return

        if self._client is None:
            return

        self._client.refresh_access_token_from_disk()
        pnl = self._client.get_portfolio_day_pnl()
        if pnl is None:
            return

        publish_upstox_pnl_snapshot(pnl)

        state = load_state()
        entries = int(state.get("entries_today") or 0)
        expiry_day = is_expiry_trading_day(now.date())
        target = effective_target_inr(entries, expiry_day)
        total = float(pnl.get("total_pnl") or 0.0)

        state.update(
            {
                "upstox_pnl_inr": round(total, 2),
                "upstox_realised_inr": round(float(pnl.get("realised") or 0.0), 2),
                "upstox_unrealised_inr": round(float(pnl.get("unrealised") or 0.0), 2),
                "target_inr": target,
                "expiry_day": expiry_day,
            }
        )
        save_state(state)

        if not PROFIT_GUARD_ENABLED:
            return

        engage, hit_target, reason = should_engage_target(total, entries, expiry_day)
        if not engage:
            return

        engage_daily_profit_target(
            total_pnl_inr=total,
            target_inr=hit_target,
            reason=reason,
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ProfitTargetEngine().run()


if __name__ == "__main__":
    main()
