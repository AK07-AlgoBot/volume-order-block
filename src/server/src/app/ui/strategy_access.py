"""Strategy entitlements for dashboard panels and performance review."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.constants import (
    ALL_STRATEGIES,
    STRATEGY_GAMMA,
    STRATEGY_LABELS,
    STRATEGY_S1_OI,
    STRATEGY_S2_SMC,
    STRATEGY_S3_BREAKOUT,
    STRATEGY_S7_ORB,
    STRATEGY_S8_CHOCH,
)
from app.services.user_profiles_store import strategy_enabled

# Engine / performance-store ids → dashboard entitlement ids
_TRADE_STRATEGY_ID_MAP: dict[str, str] = {
    "ak07_oi": STRATEGY_S1_OI,
    "s1_oi": STRATEGY_S1_OI,
    "smc_crt": STRATEGY_S2_SMC,
    "s2_smc": STRATEGY_S2_SMC,
    "breakout": STRATEGY_S3_BREAKOUT,
    "s3_breakout": STRATEGY_S3_BREAKOUT,
    "s7_orb": STRATEGY_S7_ORB,
    "s8_choch": STRATEGY_S8_CHOCH,
    "gamma": STRATEGY_GAMMA,
}


def enabled_strategy_ids() -> list[str]:
    from app.ui.auth_session import current_profile, is_admin

    if is_admin():
        return list(ALL_STRATEGIES)
    prof = current_profile()
    return [s for s in (prof.get("enabled_strategies") or []) if s in ALL_STRATEGIES]


def enabled_strategy_labels_text() -> str:
    labels = [STRATEGY_LABELS.get(s, s) for s in enabled_strategy_ids()]
    return " · ".join(labels) if labels else "No strategies assigned"


def user_can_view_strategy(strategy_id: str) -> bool:
    from app.ui.auth_session import current_profile, current_role

    return strategy_enabled(current_profile(), strategy_id, role=current_role())


def _entitlement_for_trade(trade: dict[str, Any]) -> str | None:
    raw_id = str(trade.get("strategy_id") or "").strip()
    if raw_id:
        mapped = _TRADE_STRATEGY_ID_MAP.get(raw_id, raw_id)
        if mapped in ALL_STRATEGIES:
            return mapped
    strategy_name = str(trade.get("strategy") or "").strip()
    if strategy_name:
        for sid, label in STRATEGY_LABELS.items():
            if strategy_name == label:
                return sid
    return None


def user_can_view_trade(trade: dict[str, Any]) -> bool:
    """Non-admins: assigned strategies only, and only trades attributed to them."""
    from app.ui.auth_session import current_username, is_admin

    entitlement = _entitlement_for_trade(trade)
    if entitlement is None:
        return is_admin()
    if not user_can_view_strategy(entitlement):
        return False
    if is_admin():
        return True
    trade_user = str(trade.get("username") or "").strip()
    # Hide shared/historical unattributed rows from onboarded users.
    if not trade_user:
        return False
    return trade_user == str(current_username() or "").strip()


def performance_start_floor() -> date | None:
    """Non-admin: clamp review range to onboarding day. Admin: no floor."""
    from app.ui.auth_session import current_role, current_username, is_admin
    from app.services.user_profiles_store import profile_created_date

    if is_admin():
        return None
    username = str(current_username() or "").strip()
    if not username:
        return date.today()
    return profile_created_date(username, role=str(current_role() or "user"))


def tabbed_dashboard_index_codes() -> list[str]:
    """Indices for Nifty / BankNifty / Sensex tabs (S3 is Nifty-only and sits above tabs)."""
    from app.services.upstox_engine import INDEX_CONFIGS

    codes: set[str] = set()
    if user_can_view_strategy(STRATEGY_S1_OI):
        codes.update(INDEX_CONFIGS.keys())
    if user_can_view_strategy(STRATEGY_S2_SMC):
        from app.services.smc_crt_engine import SMC_CRT_INSTRUMENTS

        codes.update(SMC_CRT_INSTRUMENTS.keys())
    if user_can_view_strategy(STRATEGY_S7_ORB):
        codes.update(INDEX_CONFIGS.keys())
    if user_can_view_strategy(STRATEGY_S8_CHOCH):
        from app.services.choch_engine import CHOCH_INSTRUMENTS

        codes.update(CHOCH_INSTRUMENTS)
    if user_can_view_strategy(STRATEGY_GAMMA):
        codes.update(INDEX_CONFIGS.keys())
    return [code for code in INDEX_CONFIGS if code in codes]
