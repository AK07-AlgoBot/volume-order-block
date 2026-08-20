"""Per-user S3 order fan-out — route live orders to each trader's broker account."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Final

from app.constants import STRATEGY_COPY_KITE, STRATEGY_GC_OF, STRATEGY_S29_ORB, STRATEGY_S3_BREAKOUT
from app.services.groww_engine import GrowwClient
from app.services.kite_engine import KiteClient
from app.services.upstox_engine import UpstoxClient, build_upstox_client

logger = logging.getLogger("ak07.breakout_fanout")

# futures = index FUT (legacy). options = BUY ITM CE/PE, exit with SELL (lower brokerage).
S3_EXEC_INSTRUMENT: Final[str] = (os.environ.get("BREAKOUT_EXEC_INSTRUMENT") or "options").strip().lower()

# Fan-out to every live (non-paper) user, ignoring strategy entitlements.
FANOUT_ALL_LIVE: Final[str] = "*"


@dataclass(frozen=True)
class S3Trader:
    username: str
    broker: str
    lots: int = 1


def list_live_traders(
    strategy_id: str,
    *,
    lots_strategy_id: str | None = None,
    exclude_usernames: frozenset[str] | set[str] | None = None,
) -> list[S3Trader]:
    """Live (non-paper) users who have this strategy enabled.

    Pass ``FANOUT_ALL_LIVE`` (``"*"``) to skip the entitlement filter.
    """
    from app.services.user_profiles_store import lots_for_strategy, read_profile
    from app.services.users_store import list_users

    skip = {str(n).strip().lower() for n in (exclude_usernames or ()) if str(n).strip()}
    if strategy_id == FANOUT_ALL_LIVE:
        lot_sid = lots_strategy_id or STRATEGY_GC_OF
    else:
        lot_sid = lots_strategy_id or strategy_id
    traders: list[S3Trader] = []
    for row in list_users():
        username = str(row.get("username") or "")
        role = str(row.get("role") or "user")
        if not username:
            continue
        if username.strip().lower() in skip:
            continue
        profile = read_profile(username, role=role)
        enabled = profile.get("enabled_strategies") or []
        if strategy_id != FANOUT_ALL_LIVE and strategy_id not in enabled:
            continue
        if profile.get("paper_trading"):
            continue
        broker = str(profile.get("broker") or "upstox").strip().lower()
        traders.append(
            S3Trader(
                username=username,
                broker=broker,
                lots=lots_for_strategy(profile, lot_sid),
            )
        )
    return traders


def list_live_s3_traders() -> list[S3Trader]:
    return list_live_traders(STRATEGY_S3_BREAKOUT)


def s3_uses_options() -> bool:
    return S3_EXEC_INSTRUMENT in ("option", "options", "opt")


def _entry_side(direction: str, *, options: bool) -> str:
    if options:
        return "BUY"  # long CE on LONG signal, long PE on SHORT signal
    return "BUY" if direction == "LONG" else "SELL"


def _exit_side(direction: str, *, options: bool) -> str:
    if options:
        return "SELL"
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
    spot: float | None = None,
    strategy_id: str = STRATEGY_S3_BREAKOUT,
    log_tag: str = "S3",
    force_options: bool | None = None,
) -> list[dict[str, Any]]:
    """Place entry on every live trader's broker for this strategy. Returns successful legs."""
    options = s3_uses_options() if force_options is None else bool(force_options)
    if options and (spot is None or spot <= 0):
        logger.error("%s options entry aborted — spot required to pick ITM strike", log_tag)
        return []

    if global_paper:
        traders = list_live_traders(strategy_id)
        if only_usernames:
            traders = [t for t in traders if t.username in only_usernames]
        label = f"{index_code} OPT" if options else f"{index_code} FUT"
        return [
            {
                "username": t.username,
                "broker": t.broker,
                "trading_symbol": label,
                "instrument_key": "",
                "quantity": lot_size * t.lots,
                "lots": t.lots,
                "paper": True,
                "instrument_kind": "options" if options else "futures",
            }
            for t in traders
        ]

    side = _entry_side(direction, options=options)
    legs: list[dict[str, Any]] = []

    # One shared strike for ALL brokers (Upstox delta pick). Prevents Kite/Groww
    # ITM-first from choosing a different strike so trail/SL sees one premium path.
    shared_strike: int | None = None
    shared_opt: str | None = None
    shared_upstox_contract: dict[str, Any] | None = None
    if options and upstox_market_client is not None:
        from app.services.upstox_engine import INDEX_CONFIGS

        cfg = INDEX_CONFIGS.get(index_code.upper())
        if cfg:
            shared_upstox_contract = upstox_market_client.get_itm_option_contract(
                cfg.spot_instrument_key, float(spot), direction
            )
            if shared_upstox_contract:
                shared_strike = int(shared_upstox_contract.get("strike") or 0) or None
                shared_opt = str(shared_upstox_contract.get("option_type") or "") or None
                logger.info(
                    "%s shared option strike %s%s (selection=%s) — all brokers must use this",
                    log_tag,
                    shared_strike,
                    shared_opt,
                    shared_upstox_contract.get("selection"),
                )

    for trader in list_live_traders(strategy_id):
        if only_usernames and trader.username not in only_usernames:
            continue
        trader_lots = max(1, int(trader.lots or lots or 1))
        quantity = lot_size * trader_lots
        if trader.broker == "groww":
            groww = GrowwClient(trader.username)
            if not groww.has_token():
                logger.error("[%s] %s entry skipped — no Groww token", trader.username, log_tag)
                continue
            if options:
                contract = groww.get_itm_option_contract(
                    index_code, float(spot), direction, force_strike=shared_strike
                )
            else:
                contract = groww.get_index_future_contract(index_code)
            if not contract:
                kind = "option" if options else "future"
                logger.error("[%s] %s entry skipped — no Groww %s %s", trader.username, log_tag, index_code, kind)
                continue
            qty = int(contract.get("lot_size") or lot_size) * trader_lots
            sym = str(contract["trading_symbol"])
            # Options are always long premium → treat as LONG exposure check
            expose_dir = "LONG" if options else direction
            if groww.has_directional_exposure(sym, expose_dir, qty):
                logger.warning(
                    "[%s] Groww already has exposure on %s — skipping duplicate entry",
                    trader.username,
                    sym,
                )
                legs.append(
                    _groww_entry_leg_from_position(
                        trader, contract, direction, trader_lots, options=options
                    )
                )
                continue
            order_id = groww.place_market_order(
                sym,
                qty,
                side,
                exchange=str(contract.get("exchange") or "NSE"),
            )
            if not order_id:
                logger.error("[%s] Groww %s entry order failed", trader.username, log_tag)
                continue
            premium = groww.get_fno_ltp(sym)
            legs.append(
                {
                    "username": trader.username,
                    "broker": "groww",
                    "trading_symbol": contract["trading_symbol"],
                    "groww_symbol": contract.get("groww_symbol"),
                    "instrument_key": contract.get("instrument_key") or "",
                    "contract_label": contract.get("contract_label") or "",
                    "quantity": qty,
                    "lots": trader_lots,
                    "groww_order_id": order_id,
                    "instrument_kind": "options" if options else "futures",
                    "option_strike": int(contract.get("strike") or 0),
                    "option_type": str(contract.get("option_type") or ""),
                    "premium_entry": float(premium) if premium is not None else None,
                    "selection": contract.get("selection"),
                }
            )
            logger.info(
                "[%s] %s Groww entry %s %d x %s (%s)%s",
                trader.username,
                log_tag,
                side,
                qty,
                contract["trading_symbol"],
                order_id,
                f" premium≈{premium}" if premium is not None else "",
            )
            continue

        if trader.broker == "kite":
            kite = KiteClient(trader.username)
            if not kite.has_token():
                logger.error("[%s] %s entry skipped — no Kite token", trader.username, log_tag)
                continue
            if options:
                contract = kite.get_itm_option_contract(
                    index_code, float(spot), direction, force_strike=shared_strike
                )
            else:
                contract = kite.get_index_future_contract(index_code)
            if not contract:
                kind = "option" if options else "future"
                logger.error("[%s] %s entry skipped — no Kite %s %s", trader.username, log_tag, index_code, kind)
                continue
            qty = int(contract.get("lot_size") or lot_size) * trader_lots
            sym = str(contract["tradingsymbol"])
            exchange = str(contract.get("exchange") or "NFO")
            expose_dir = "LONG" if options else direction
            if kite.has_directional_exposure(exchange, sym, expose_dir, qty):
                logger.warning(
                    "[%s] Kite already has exposure on %s:%s — skipping duplicate entry",
                    trader.username,
                    exchange,
                    sym,
                )
                legs.append(
                    _kite_entry_leg_from_position(
                        trader, contract, direction, trader_lots, options=options
                    )
                )
                continue
            order_id = kite.place_market_order(exchange, sym, qty, side)
            if not order_id:
                logger.error("[%s] Kite %s entry order failed", trader.username, log_tag)
                continue
            premium = kite.get_fno_ltp(exchange, sym)
            legs.append(
                {
                    "username": trader.username,
                    "broker": "kite",
                    "trading_symbol": sym,
                    "exchange": exchange,
                    "instrument_key": contract.get("instrument_key") or "",
                    "contract_label": contract.get("contract_label") or "",
                    "quantity": qty,
                    "lots": trader_lots,
                    "kite_order_id": order_id,
                    "instrument_kind": "options" if options else "futures",
                    "option_strike": int(contract.get("strike") or 0),
                    "option_type": str(contract.get("option_type") or ""),
                    "premium_entry": float(premium) if premium is not None else None,
                    "selection": contract.get("selection"),
                }
            )
            logger.info(
                "[%s] %s Kite entry %s %d x %s:%s (%s)%s",
                trader.username,
                log_tag,
                side,
                qty,
                exchange,
                sym,
                order_id,
                f" premium≈{premium}" if premium is not None else "",
            )
            continue

        if trader.broker == "upstox":
            upstox = build_upstox_client(trader.username)
            if options:
                from app.services.upstox_engine import INDEX_CONFIGS

                cfg = INDEX_CONFIGS.get(index_code.upper())
                if not cfg:
                    logger.error("[%s] %s entry skipped — unknown index %s", trader.username, log_tag, index_code)
                    continue
                client = upstox_market_client or upstox
                contract = shared_upstox_contract or client.get_itm_option_contract(
                    cfg.spot_instrument_key, float(spot), direction
                )
                if not contract or not contract.get("instrument_key"):
                    logger.error("[%s] %s entry skipped — no Upstox %s ITM option", trader.username, log_tag, index_code)
                    continue
                ok = upstox.place_market_order(str(contract["instrument_key"]), quantity, "BUY")
                if not ok:
                    logger.error("[%s] Upstox %s option entry failed", trader.username, log_tag)
                    continue
                premium = upstox.get_ltp(str(contract["instrument_key"]))
                label = f"{contract['strike']}{contract['option_type']}"
                legs.append(
                    {
                        "username": trader.username,
                        "broker": "upstox",
                        "trading_symbol": label,
                        "instrument_key": contract["instrument_key"],
                        "contract_label": label,
                        "quantity": quantity,
                        "lots": trader_lots,
                        "instrument_kind": "options",
                        "option_strike": int(contract["strike"]),
                        "option_type": str(contract["option_type"]),
                        "premium_entry": float(premium) if premium is not None else None,
                        "delta": contract.get("delta"),
                        "abs_delta": contract.get("abs_delta"),
                        "selection": contract.get("selection") or "shared_s3_strike",
                    }
                )
                logger.info(
                    "[%s] %s Upstox entry BUY %d x %s δ=%s%s",
                    trader.username,
                    log_tag,
                    quantity,
                    label,
                    contract.get("abs_delta") or contract.get("delta"),
                    f" premium≈{premium}" if premium is not None else "",
                )
                continue

            contract = upstox.get_index_future_contract(index_code)
            if not contract or not contract.get("instrument_key"):
                logger.error("[%s] %s entry skipped — no Upstox %s future", trader.username, log_tag, index_code)
                continue
            ok = upstox.place_market_order(str(contract["instrument_key"]), quantity, side)
            if not ok:
                logger.error("[%s] Upstox %s entry order failed", trader.username, log_tag)
                continue
            legs.append(
                {
                    "username": trader.username,
                    "broker": "upstox",
                    "trading_symbol": contract.get("trading_symbol") or contract["instrument_key"],
                    "instrument_key": contract["instrument_key"],
                    "contract_label": contract.get("contract_label") or "",
                    "quantity": quantity,
                    "lots": trader_lots,
                    "instrument_kind": "futures",
                }
            )
            logger.info(
                "[%s] %s Upstox entry %s %d x %s",
                trader.username,
                log_tag,
                side,
                quantity,
                contract.get("trading_symbol") or contract["instrument_key"],
            )
            continue

        logger.error("[%s] %s entry skipped — unsupported broker %s", trader.username, log_tag, trader.broker)

    return legs


def _kite_entry_leg_from_position(
    trader: S3Trader,
    contract: dict[str, Any],
    direction: str,
    lots: int,
    *,
    options: bool = False,
) -> dict[str, Any]:
    """Synthetic leg when Kite already holds the S3 entry (prevents duplicate orders)."""
    qty = int(contract.get("lot_size") or 65) * lots
    return {
        "username": trader.username,
        "broker": "kite",
        "trading_symbol": contract["tradingsymbol"],
        "exchange": contract.get("exchange") or "NFO",
        "instrument_key": contract.get("instrument_key") or "",
        "contract_label": contract.get("contract_label") or "",
        "quantity": qty,
        "lots": lots,
        "kite_order_id": "existing_position",
        "recovered": True,
        "instrument_kind": "options" if options else "futures",
        "option_strike": int(contract.get("strike") or 0),
        "option_type": str(contract.get("option_type") or ""),
    }


def _groww_entry_leg_from_position(
    trader: S3Trader,
    contract: dict[str, Any],
    direction: str,
    lots: int,
    *,
    options: bool = False,
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
        "lots": lots,
        "groww_order_id": "existing_position",
        "recovered": True,
        "instrument_kind": "options" if options else "futures",
        "option_strike": int(contract.get("strike") or 0),
        "option_type": str(contract.get("option_type") or ""),
    }


def missing_s3_traders(
    existing_legs: list[dict[str, Any]],
    *,
    assume_upstox_filled: bool,
    index_code: str | None = None,
    direction: str | None = None,
    lot_size: int = 65,
    lots: int = 1,
    exclude_usernames: frozenset[str] | set[str] | None = None,
    strategy_id: str = STRATEGY_S3_BREAKOUT,
    force_options: bool | None = None,
) -> list[S3Trader]:
    """Live traders who did not get an entry leg yet.

    ``exclude_usernames`` — already entered this position (even if leg was later
    removed). Prevents catch-up from re-buying after a partial kill.
    """
    covered = {str(leg.get("username") or "") for leg in existing_legs if leg.get("username")}
    if exclude_usernames:
        for name in exclude_usernames:
            n = str(name or "").strip()
            if n:
                covered.add(n)
    # Case-insensitive cover check
    covered_l = {c.lower() for c in covered if c}
    if assume_upstox_filled and not covered:
        for trader in list_live_traders(strategy_id):
            if trader.broker == "upstox":
                covered.add(trader.username)
                covered_l.add(trader.username.lower())
    options = s3_uses_options() if force_options is None else bool(force_options)
    if index_code and direction and not options:
        for trader in list_live_traders(strategy_id):
            if trader.username in covered or trader.username.lower() in covered_l:
                continue
            trader_lots = max(1, int(trader.lots or lots or 1))
            if trader.broker == "groww":
                groww = GrowwClient(trader.username)
                contract = groww.get_index_future_contract(index_code)
                if not contract:
                    continue
                sym = str(contract.get("trading_symbol") or "")
                need = int(contract.get("lot_size") or lot_size) * trader_lots
                if groww.has_directional_exposure(sym, direction, need):
                    logger.info(
                        "[%s] Groww already has %s %s on %s — treating as covered",
                        trader.username,
                        direction,
                        sym,
                        need,
                    )
                    covered.add(trader.username)
                    covered_l.add(trader.username.lower())
                continue
            kite = KiteClient(trader.username)
            contract = kite.get_index_future_contract(index_code)
            if not contract:
                continue
            sym = str(contract.get("tradingsymbol") or "")
            exchange = str(contract.get("exchange") or "NFO")
            need = int(contract.get("lot_size") or lot_size) * trader_lots
            if kite.has_directional_exposure(exchange, sym, direction, need):
                logger.info(
                    "[%s] Kite already has %s %s:%s — treating as covered",
                    trader.username,
                    direction,
                    exchange,
                    sym,
                )
                covered.add(trader.username)
                covered_l.add(trader.username.lower())
    return [
        t
        for t in list_live_traders(strategy_id)
        if t.username not in covered and t.username.lower() not in covered_l
    ]


def catchup_s3_legs(
    *,
    index_code: str,
    direction: str,
    lot_size: int,
    lots: int,
    existing_legs: list[dict[str, Any]],
    upstox_market_client: UpstoxClient | None,
    global_paper: bool,
    spot: float | None = None,
    exclude_usernames: frozenset[str] | set[str] | None = None,
    strategy_id: str = STRATEGY_S3_BREAKOUT,
    log_tag: str = "S3",
    force_options: bool | None = None,
) -> list[dict[str, Any]]:
    """Place entries for live traders missing from an open position's legs."""
    missing = missing_s3_traders(
        existing_legs,
        assume_upstox_filled=not existing_legs,
        index_code=index_code,
        direction=direction,
        lot_size=lot_size,
        lots=lots,
        exclude_usernames=exclude_usernames,
        strategy_id=strategy_id,
        force_options=force_options,
    )
    if not missing:
        return []
    names = frozenset(t.username for t in missing)
    logger.info("%s catch-up entry for missing traders: %s", log_tag, ", ".join(sorted(names)))
    return place_s3_entries(
        index_code=index_code,
        direction=direction,
        lot_size=lot_size,
        lots=lots,
        upstox_market_client=upstox_market_client,
        global_paper=global_paper,
        only_usernames=names,
        spot=spot,
        strategy_id=strategy_id,
        log_tag=log_tag,
        force_options=force_options,
    )


def place_s3_exits(
    legs: list[dict[str, Any]],
    direction: str,
    *,
    global_paper: bool,
    log_tag: str = "S3",
) -> bool:
    """Exit all legs placed at entry. Returns True if every leg succeeded."""
    if global_paper or not legs:
        return True

    options = any(str(leg.get("instrument_kind") or "") == "options" for leg in legs) or s3_uses_options()
    side = _exit_side(direction, options=options)
    all_ok = True

    for leg in legs:
        if leg.get("paper"):
            continue
        username = str(leg.get("username") or "")
        broker = str(leg.get("broker") or "upstox")
        qty = int(leg.get("quantity") or 0)
        if qty <= 0:
            continue
        leg_options = str(leg.get("instrument_kind") or "") == "options" or options

        if broker == "groww":
            groww = GrowwClient(username)
            trading_symbol = str(leg.get("trading_symbol") or "")
            if not trading_symbol:
                logger.error("[%s] Groww exit skipped — missing trading_symbol", username)
                all_ok = False
                continue
            net = groww.net_fno_quantity(trading_symbol)
            # Options: we are always long premium → flat when net <= 0
            if leg_options:
                if net <= 0:
                    logger.warning(
                        "[%s] Groww option exit skipped — already flat on %s (net=%d)",
                        username,
                        trading_symbol,
                        net,
                    )
                    continue
            else:
                if direction == "LONG" and net <= 0:
                    logger.warning(
                        "[%s] Groww exit skipped — already flat/short on %s (net=%d)",
                        username,
                        trading_symbol,
                        net,
                    )
                    continue
                if direction == "SHORT" and net >= 0:
                    logger.warning(
                        "[%s] Groww exit skipped — already flat/long on %s (net=%d)",
                        username,
                        trading_symbol,
                        net,
                    )
                    continue
            exit_qty = min(qty, abs(net)) if net != 0 else qty
            order_id = groww.place_market_order(trading_symbol, exit_qty, side)
            if not order_id:
                logger.error("[%s] Groww %s exit order failed", username, log_tag)
                all_ok = False
            else:
                logger.info(
                    "[%s] %s Groww exit %s %d x %s (%s)",
                    username,
                    log_tag,
                    side,
                    exit_qty,
                    trading_symbol,
                    order_id,
                )
            continue

        if broker == "kite":
            kite = KiteClient(username)
            trading_symbol = str(leg.get("trading_symbol") or "")
            exchange = str(leg.get("exchange") or "NFO")
            if not trading_symbol:
                logger.error("[%s] Kite exit skipped — missing tradingsymbol", username)
                all_ok = False
                continue
            net = kite.net_fno_quantity(exchange, trading_symbol)
            if leg_options:
                if net <= 0:
                    logger.warning(
                        "[%s] Kite option exit skipped — already flat on %s:%s (net=%d)",
                        username,
                        exchange,
                        trading_symbol,
                        net,
                    )
                    continue
            else:
                if direction == "LONG" and net <= 0:
                    logger.warning(
                        "[%s] Kite exit skipped — already flat/short on %s:%s (net=%d)",
                        username,
                        exchange,
                        trading_symbol,
                        net,
                    )
                    continue
                if direction == "SHORT" and net >= 0:
                    logger.warning(
                        "[%s] Kite exit skipped — already flat/long on %s:%s (net=%d)",
                        username,
                        exchange,
                        trading_symbol,
                        net,
                    )
                    continue
            exit_qty = min(qty, abs(net)) if net != 0 else qty
            order_id = kite.place_market_order(
                exchange,
                trading_symbol,
                exit_qty,
                side,
                bypass_profit_guard=True,
            )
            if not order_id:
                logger.error("[%s] Kite %s exit order failed", username, log_tag)
                all_ok = False
            else:
                logger.info(
                    "[%s] %s Kite exit %s %d x %s:%s (%s)",
                    username,
                    log_tag,
                    side,
                    exit_qty,
                    exchange,
                    trading_symbol,
                    order_id,
                )
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
                logger.error("[%s] Upstox %s exit order failed", username, log_tag)
                all_ok = False
            else:
                logger.info("[%s] %s Upstox exit %s %d x %s", username, log_tag, side, qty, instrument_key)
            continue

        logger.warning("[%s] %s exit skipped — broker %s not wired", username, log_tag, broker)
        all_ok = False

    return all_ok


def legs_summary(legs: list[dict[str, Any]]) -> str:
    if not legs:
        return "none"
    bits: list[str] = []
    for leg in legs:
        user = leg.get("username", "?")
        broker = leg.get("broker", "?")
        label = leg.get("contract_label") or leg.get("trading_symbol") or ""
        bits.append(f"{user}@{broker} ({label})")
    return ", ".join(bits)


def leg_usernames(legs: list[dict[str, Any]] | None) -> list[str]:
    """Unique follower names from fan-out legs, first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for leg in legs or []:
        if not isinstance(leg, dict):
            continue
        name = str(leg.get("username") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def position_legs(pos: Any) -> list[dict[str, Any]]:
    legs = getattr(pos, "order_legs", None)
    return legs if isinstance(legs, list) else []


def place_s29_entries(
    *,
    index_code: str,
    direction: str,
    lot_size: int,
    lots: int,
    upstox_market_client: UpstoxClient | None,
    global_paper: bool,
    only_usernames: frozenset[str] | None = None,
    spot: float | None = None,
) -> list[dict[str, Any]]:
    """S29 Nifty ORB+ — same per-user broker fan-out as S3, options only."""
    return place_s3_entries(
        index_code=index_code,
        direction=direction,
        lot_size=lot_size,
        lots=lots,
        upstox_market_client=upstox_market_client,
        global_paper=global_paper,
        only_usernames=only_usernames,
        spot=spot,
        strategy_id=STRATEGY_S29_ORB,
        log_tag="S29",
        force_options=True,
    )


def place_s29_exits(
    legs: list[dict[str, Any]],
    direction: str,
    *,
    global_paper: bool,
) -> bool:
    return place_s3_exits(legs, direction, global_paper=global_paper, log_tag="S29")


def catchup_s29_legs(
    *,
    index_code: str,
    direction: str,
    lot_size: int,
    lots: int,
    existing_legs: list[dict[str, Any]],
    upstox_market_client: UpstoxClient | None,
    global_paper: bool,
    spot: float | None = None,
    exclude_usernames: frozenset[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    return catchup_s3_legs(
        index_code=index_code,
        direction=direction,
        lot_size=lot_size,
        lots=lots,
        existing_legs=existing_legs,
        upstox_market_client=upstox_market_client,
        global_paper=global_paper,
        spot=spot,
        exclude_usernames=exclude_usernames,
        strategy_id=STRATEGY_S29_ORB,
        log_tag="S29",
        force_options=True,
    )


def _gc_fanout_strategy_id() -> str:
    raw = (os.environ.get("AK07_GC_FANOUT_ALL_LIVE") or "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return STRATEGY_GC_OF
    return FANOUT_ALL_LIVE


def place_gc_entries(
    *,
    index_code: str,
    direction: str,
    lot_size: int,
    lots: int,
    upstox_market_client: UpstoxClient | None,
    global_paper: bool,
    only_usernames: frozenset[str] | None = None,
    spot: float | None = None,
) -> list[dict[str, Any]]:
    """GoCharting orderflow — S3 ITM CE/PE fan-out to live users."""
    return place_s3_entries(
        index_code=index_code,
        direction=direction,
        lot_size=lot_size,
        lots=lots,
        upstox_market_client=upstox_market_client,
        global_paper=global_paper,
        only_usernames=only_usernames,
        spot=spot,
        strategy_id=_gc_fanout_strategy_id(),
        log_tag="GC",
        force_options=True,
    )


def place_gc_exits(
    legs: list[dict[str, Any]],
    direction: str,
    *,
    global_paper: bool,
) -> bool:
    return place_s3_exits(legs, direction, global_paper=global_paper, log_tag="GC")


def catchup_gc_legs(
    *,
    index_code: str,
    direction: str,
    lot_size: int,
    lots: int,
    existing_legs: list[dict[str, Any]],
    upstox_market_client: UpstoxClient | None,
    global_paper: bool,
    spot: float | None = None,
    exclude_usernames: frozenset[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    return catchup_s3_legs(
        index_code=index_code,
        direction=direction,
        lot_size=lot_size,
        lots=lots,
        existing_legs=existing_legs,
        upstox_market_client=upstox_market_client,
        global_paper=global_paper,
        spot=spot,
        exclude_usernames=exclude_usernames,
        strategy_id=_gc_fanout_strategy_id(),
        log_tag="GC",
        force_options=True,
    )


def _copy_fanout_strategy_id() -> str:
    raw = (os.environ.get("AK07_COPY_FANOUT_ALL_LIVE") or "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return STRATEGY_COPY_KITE
    return FANOUT_ALL_LIVE


def place_copy_orders(
    *,
    index_code: str,
    side: str,
    lot_size: int,
    kite_exchange: str,
    kite_symbol: str,
    instrument_kind: str,
    option_strike: int,
    option_type: str,
    expiry: str,
    upstox_market_client: UpstoxClient | None,
    global_paper: bool,
    exclude_usernames: frozenset[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    """Copy a filled Kite FNO order onto other live accounts (Upstox OMS + each broker)."""
    log_tag = "COPY"
    side_u = side.upper()
    exclude = frozenset(str(n).strip() for n in (exclude_usernames or ()) if str(n).strip())
    traders = list_live_traders(
        _copy_fanout_strategy_id(),
        lots_strategy_id=STRATEGY_COPY_KITE,
        exclude_usernames=exclude,
    )
    if global_paper:
        return [
            {
                "username": t.username,
                "broker": t.broker,
                "trading_symbol": kite_symbol,
                "quantity": lot_size * t.lots,
                "lots": t.lots,
                "paper": True,
                "instrument_kind": instrument_kind,
            }
            for t in traders
        ]

    shared_upstox: dict[str, Any] | None = None
    if instrument_kind == "options" and upstox_market_client is not None:
        shared_upstox = upstox_market_client.get_option_contract_exact(
            index_code, expiry=expiry, strike=option_strike, option_type=option_type
        )
        if not shared_upstox:
            logger.error(
                "%s no Upstox %s %s%d %s — aborting fan-out",
                log_tag,
                index_code,
                option_type,
                option_strike,
                expiry,
            )
            return []

    legs: list[dict[str, Any]] = []
    for trader in traders:
        trader_lots = max(1, int(trader.lots or 1))
        quantity = lot_size * trader_lots
        if trader.broker == "upstox":
            upstox = build_upstox_client(trader.username)
            if instrument_kind == "options":
                contract = shared_upstox
                if not contract or not contract.get("instrument_key"):
                    logger.error("[%s] %s skipped — no Upstox option key", trader.username, log_tag)
                    continue
                ok = upstox.place_market_order(str(contract["instrument_key"]), quantity, side_u)
                if not ok:
                    logger.error("[%s] Upstox %s order failed", trader.username, log_tag)
                    continue
                premium = upstox.get_ltp(str(contract["instrument_key"]))
                label = f"{contract['strike']}{contract['option_type']}"
                legs.append(
                    {
                        "username": trader.username,
                        "broker": "upstox",
                        "trading_symbol": label,
                        "instrument_key": contract["instrument_key"],
                        "quantity": quantity,
                        "lots": trader_lots,
                        "instrument_kind": "options",
                        "option_strike": int(contract["strike"]),
                        "option_type": str(contract["option_type"]),
                        "premium_entry": float(premium) if premium is not None else None,
                    }
                )
                logger.info("[%s] %s Upstox %s %d x %s", trader.username, log_tag, side_u, quantity, label)
                continue
            fut = upstox.get_index_future_contract(index_code)
            if not fut or not fut.get("instrument_key"):
                logger.error("[%s] %s skipped — no Upstox future", trader.username, log_tag)
                continue
            ok = upstox.place_market_order(str(fut["instrument_key"]), quantity, side_u)
            if not ok:
                logger.error("[%s] Upstox %s future order failed", trader.username, log_tag)
                continue
            legs.append(
                {
                    "username": trader.username,
                    "broker": "upstox",
                    "trading_symbol": fut.get("trading_symbol") or "",
                    "instrument_key": fut["instrument_key"],
                    "quantity": quantity,
                    "lots": trader_lots,
                    "instrument_kind": "futures",
                }
            )
            logger.info(
                "[%s] %s Upstox %s %d x %s",
                trader.username,
                log_tag,
                side_u,
                quantity,
                fut.get("trading_symbol"),
            )
            continue

        if trader.broker == "kite":
            kite = KiteClient(trader.username)
            if not kite.has_token():
                logger.error("[%s] %s skipped — no Kite token", trader.username, log_tag)
                continue
            order_id = kite.place_market_order(kite_exchange, kite_symbol, quantity, side_u)
            if not order_id:
                logger.error("[%s] Kite %s order failed", trader.username, log_tag)
                continue
            legs.append(
                {
                    "username": trader.username,
                    "broker": "kite",
                    "trading_symbol": kite_symbol,
                    "exchange": kite_exchange,
                    "quantity": quantity,
                    "lots": trader_lots,
                    "kite_order_id": order_id,
                    "instrument_kind": instrument_kind,
                    "option_strike": option_strike,
                    "option_type": option_type,
                }
            )
            logger.info(
                "[%s] %s Kite %s %d x %s:%s (%s)",
                trader.username,
                log_tag,
                side_u,
                quantity,
                kite_exchange,
                kite_symbol,
                order_id,
            )
            continue

        if trader.broker == "groww":
            groww = GrowwClient(trader.username)
            if not groww.has_token():
                logger.error("[%s] %s skipped — no Groww token", trader.username, log_tag)
                continue
            if instrument_kind == "options":
                contract = groww.get_option_contract_exact(
                    index_code, expiry=expiry, strike=option_strike, option_type=option_type
                )
            else:
                contract = groww.get_index_future_contract(index_code)
            if not contract:
                logger.error("[%s] %s skipped — no Groww contract", trader.username, log_tag)
                continue
            qty = int(contract.get("lot_size") or lot_size) * trader_lots
            sym = str(contract.get("trading_symbol") or "")
            order_id = groww.place_market_order(
                sym,
                qty,
                side_u,
                exchange=str(contract.get("exchange") or "NSE"),
            )
            if not order_id:
                logger.error("[%s] Groww %s order failed", trader.username, log_tag)
                continue
            legs.append(
                {
                    "username": trader.username,
                    "broker": "groww",
                    "trading_symbol": sym,
                    "quantity": qty,
                    "lots": trader_lots,
                    "groww_order_id": order_id,
                    "instrument_kind": instrument_kind,
                    "option_strike": option_strike,
                    "option_type": option_type,
                }
            )
            logger.info("[%s] %s Groww %s %d x %s (%s)", trader.username, log_tag, side_u, qty, sym, order_id)
            continue

        logger.error("[%s] %s skipped — unsupported broker %s", trader.username, log_tag, trader.broker)
    return legs
