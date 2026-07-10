"""Per-user S3 order fan-out — route live orders to each trader's broker account."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.constants import STRATEGY_S3_BREAKOUT
from app.services.groww_engine import GrowwClient
from app.services.upstox_engine import UpstoxClient, build_upstox_client

logger = logging.getLogger("ak07.breakout_fanout")


@dataclass(frozen=True)
class S3Trader:
    username: str
    broker: str


def list_live_s3_traders() -> list[S3Trader]:
    from app.services.user_profiles_store import read_profile
    from app.services.users_store import list_users

    traders: list[S3Trader] = []
    for row in list_users():
        username = str(row.get("username") or "")
        role = str(row.get("role") or "user")
        if not username:
            continue
        profile = read_profile(username, role=role)
        if STRATEGY_S3_BREAKOUT not in (profile.get("enabled_strategies") or []):
            continue
        if profile.get("paper_trading"):
            continue
        broker = str(profile.get("broker") or "upstox").strip().lower()
        traders.append(S3Trader(username=username, broker=broker))
    return traders


def _entry_side(direction: str) -> str:
    return "BUY" if direction == "LONG" else "SELL"


def _exit_side(direction: str) -> str:
    return "SELL" if direction == "LONG" else "BUY"


def place_s3_entries(
    *,
    index_code: str,
    direction: str,
    lot_size: int,
    lots: int,
    upstox_market_client: UpstoxClient | None,
    global_paper: bool,
    only_usernames: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Place entry on every live S3 trader's broker. Returns successful legs."""
    if global_paper:
        traders = list_live_s3_traders()
        if only_usernames:
            traders = [t for t in traders if t.username in only_usernames]
        return [
            {
                "username": t.username,
                "broker": t.broker,
                "trading_symbol": f"{index_code} FUT",
                "instrument_key": "",
                "quantity": lot_size * lots,
                "paper": True,
            }
            for t in traders
        ]

    quantity = lot_size * lots
    side = _entry_side(direction)
    legs: list[dict[str, Any]] = []

    for trader in list_live_s3_traders():
        if only_usernames and trader.username not in only_usernames:
            continue
        if trader.broker == "groww":
            groww = GrowwClient(trader.username)
            if not groww.has_token():
                logger.error("[%s] S3 entry skipped — no Groww token", trader.username)
                continue
            contract = groww.get_index_future_contract(index_code)
            if not contract:
                logger.error("[%s] S3 entry skipped — no Groww %s future", trader.username, index_code)
                continue
            qty = int(contract.get("lot_size") or lot_size) * lots
            sym = str(contract["trading_symbol"])
            if groww.has_directional_exposure(sym, direction, qty):
                logger.warning(
                    "[%s] Groww already has %s exposure on %s — skipping duplicate entry",
                    trader.username,
                    direction,
                    sym,
                )
                legs.append(_groww_entry_leg_from_position(trader, contract, direction, lots))
                continue
            order_id = groww.place_market_order(
                sym,
                qty,
                side,
                exchange=str(contract.get("exchange") or "NSE"),
            )
            if not order_id:
                logger.error("[%s] Groww S3 entry order failed", trader.username)
                continue
            legs.append(
                {
                    "username": trader.username,
                    "broker": "groww",
                    "trading_symbol": contract["trading_symbol"],
                    "groww_symbol": contract.get("groww_symbol"),
                    "instrument_key": contract.get("instrument_key") or "",
                    "contract_label": contract.get("contract_label") or "",
                    "quantity": qty,
                    "groww_order_id": order_id,
                }
            )
            logger.info(
                "[%s] S3 Groww entry %s %d x %s (%s)",
                trader.username,
                side,
                qty,
                contract["trading_symbol"],
                order_id,
            )
            continue

        if trader.broker == "upstox":
            upstox = build_upstox_client(trader.username)
            contract = upstox.get_index_future_contract(index_code)
            if not contract or not contract.get("instrument_key"):
                logger.error("[%s] S3 entry skipped — no Upstox %s future", trader.username, index_code)
                continue
            ok = upstox.place_market_order(str(contract["instrument_key"]), quantity, side)
            if not ok:
                logger.error("[%s] Upstox S3 entry order failed", trader.username)
                continue
            legs.append(
                {
                    "username": trader.username,
                    "broker": "upstox",
                    "trading_symbol": contract.get("trading_symbol") or index_code,
                    "instrument_key": contract["instrument_key"],
                    "contract_label": contract.get("contract_label") or "",
                    "quantity": quantity,
                }
            )
            logger.info(
                "[%s] S3 Upstox entry %s %d x %s",
                trader.username,
                side,
                quantity,
                contract["instrument_key"],
            )
            continue

        logger.warning("[%s] S3 entry skipped — broker %s not wired for orders", trader.username, trader.broker)

    if upstox_market_client and not legs:
        logger.warning("No per-user S3 legs placed (check profiles/tokens)")
    return legs


def _groww_entry_leg_from_position(
    trader: S3Trader,
    contract: dict[str, Any],
    direction: str,
    lots: int,
) -> dict[str, Any]:
    """Synthetic leg when Groww already holds the S3 entry (prevents duplicate orders)."""
    qty = int(contract.get("lot_size") or 65) * lots
    return {
        "username": trader.username,
        "broker": "groww",
        "trading_symbol": contract["trading_symbol"],
        "groww_symbol": contract.get("groww_symbol"),
        "instrument_key": contract.get("instrument_key") or "",
        "contract_label": contract.get("contract_label") or "",
        "quantity": qty,
        "groww_order_id": "existing_position",
        "recovered": True,
    }


def missing_s3_traders(
    existing_legs: list[dict[str, Any]],
    *,
    assume_upstox_filled: bool,
    index_code: str | None = None,
    direction: str | None = None,
    lot_size: int = 65,
    lots: int = 1,
) -> list[S3Trader]:
    """Live traders who did not get an entry leg yet."""
    covered = {str(leg.get("username") or "") for leg in existing_legs if leg.get("username")}
    if assume_upstox_filled and not covered:
        for trader in list_live_s3_traders():
            if trader.broker == "upstox":
                covered.add(trader.username)
    if index_code and direction:
        qty = lot_size * lots
        for trader in list_live_s3_traders():
            if trader.username in covered or trader.broker != "groww":
                continue
            groww = GrowwClient(trader.username)
            contract = groww.get_index_future_contract(index_code)
            if not contract:
                continue
            sym = str(contract.get("trading_symbol") or "")
            need = int(contract.get("lot_size") or lot_size) * lots
            if groww.has_directional_exposure(sym, direction, need):
                logger.info(
                    "[%s] Groww already has %s %s on %s — treating as covered",
                    trader.username,
                    direction,
                    sym,
                    need,
                )
                covered.add(trader.username)
    return [t for t in list_live_s3_traders() if t.username not in covered]


def catchup_s3_legs(
    *,
    index_code: str,
    direction: str,
    lot_size: int,
    lots: int,
    existing_legs: list[dict[str, Any]],
    upstox_market_client: UpstoxClient | None,
    global_paper: bool,
) -> list[dict[str, Any]]:
    """Place entries for live traders missing from an open position's legs."""
    missing = missing_s3_traders(
        existing_legs,
        assume_upstox_filled=not existing_legs,
        index_code=index_code,
        direction=direction,
        lot_size=lot_size,
        lots=lots,
    )
    if not missing:
        return []
    names = frozenset(t.username for t in missing)
    logger.info("S3 catch-up entry for missing traders: %s", ", ".join(sorted(names)))
    return place_s3_entries(
        index_code=index_code,
        direction=direction,
        lot_size=lot_size,
        lots=lots,
        upstox_market_client=upstox_market_client,
        global_paper=global_paper,
        only_usernames=names,
    )


def place_s3_exits(
    legs: list[dict[str, Any]],
    direction: str,
    *,
    global_paper: bool,
) -> bool:
    """Exit all legs placed at entry. Returns True if every leg succeeded."""
    if global_paper or not legs:
        return True

    side = _exit_side(direction)
    all_ok = True

    for leg in legs:
        if leg.get("paper"):
            continue
        username = str(leg.get("username") or "")
        broker = str(leg.get("broker") or "upstox")
        qty = int(leg.get("quantity") or 0)
        if qty <= 0:
            continue

        if broker == "groww":
            groww = GrowwClient(username)
            trading_symbol = str(leg.get("trading_symbol") or "")
            if not trading_symbol:
                logger.error("[%s] Groww exit skipped — missing trading_symbol", username)
                all_ok = False
                continue
            order_id = groww.place_market_order(trading_symbol, qty, side)
            if not order_id:
                logger.error("[%s] Groww S3 exit order failed", username)
                all_ok = False
            else:
                logger.info("[%s] S3 Groww exit %s %d x %s (%s)", username, side, qty, trading_symbol, order_id)
            continue

        if broker == "upstox":
            upstox = build_upstox_client(username)
            instrument_key = str(leg.get("instrument_key") or "")
            if not instrument_key:
                logger.error("[%s] Upstox exit skipped — missing instrument_key", username)
                all_ok = False
                continue
            ok = upstox.place_market_order(instrument_key, qty, side, bypass_profit_guard=True)
            if not ok:
                logger.error("[%s] Upstox S3 exit order failed", username)
                all_ok = False
            else:
                logger.info("[%s] S3 Upstox exit %s %d x %s", username, side, qty, instrument_key)
            continue

        logger.warning("[%s] S3 exit skipped — broker %s not wired", username, broker)
        all_ok = False

    return all_ok


def legs_summary(legs: list[dict[str, Any]]) -> str:
    if not legs:
        return ""
    parts = []
    for leg in legs:
        label = leg.get("contract_label") or leg.get("trading_symbol") or "FUT"
        parts.append(f"{leg.get('username')}@{leg.get('broker')} ({label})")
    return ", ".join(parts)


def position_legs(position: Any) -> list[dict[str, Any]]:
    """Return order legs from position, with legacy Upstox-only fallback."""
    legs = getattr(position, "order_legs", None) or []
    if legs:
        return list(legs)
    instrument_key = str(getattr(position, "instrument_key", "") or "")
    if not instrument_key:
        return []
    return [
        {
            "username": "AK07",
            "broker": "upstox",
            "instrument_key": instrument_key,
            "trading_symbol": getattr(position, "contract_label", "") or "",
            "quantity": getattr(position, "quantity", 0),
        }
    ]
