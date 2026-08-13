"""AK07 Execution Cockpit - dark-themed Streamlit dashboard.

Reads everything from Redis (published by `upstox_engine`), so the dashboard
never blocks or couples to the engine process. The sidebar kill switch both
engages the Redis kill flag (which the engine honors on its next tick) and
transmits emergency market square-offs directly to the Upstox API.

Run:  streamlit run src/server/src/app/ui/dashboard.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.constants import (
    S3_BREAKOUT_INDICES,
    STRATEGY_GAMMA,
    STRATEGY_S1_OI,
    STRATEGY_S2_SMC,
    STRATEGY_S3_BREAKOUT,
    STRATEGY_S7_ORB,
    STRATEGY_S8_CHOCH,
)
from app.services import cache_manager
from app.services.broker_pnl_store import (
    broker_pnl_label,
    format_pnl_inr,
    get_user_broker_pnl,
    refresh_groww_pnl_if_stale,
)
from app.services.upstox_engine import INDEX_CONFIGS, INDEX_OI_RISK, DEFAULT_OI_RISK
import app.ui.auth_session as auth_session
from app.ui.strategy_access import enabled_strategy_labels_text, tabbed_dashboard_index_codes, user_can_view_strategy
from app.ui.cockpit_layout import render_compact_sidebar, render_funds_summary, render_top_status_bar
from app.ui.nav_config import build_navigation
from app.ui.styles import inject_dark_theme

REFRESH_SECONDS = 5
MOCK_MODE = os.environ.get("AK07_MOCK") == "1"
PRODUCTION_DOMAIN = (os.environ.get("PRODUCTION_DOMAIN") or "").strip() or "ak07.in"

if MOCK_MODE:
    from app.services import mock_data

    mock_data.seed()


def bias_badge(bias: str) -> str:
    css = {"BULLISH": "ak07-bull", "BEARISH": "ak07-bear"}.get(bias, "ak07-neutral")
    return f'<span class="ak07-badge {css}">{bias}</span>'


def component_block(symbol: str, pct: float | None) -> str:
    if pct is None:
        return f'<div class="ak07-block ak07-gray">{symbol}<br>n/a</div>'
    css = "ak07-green" if pct > 0 else ("ak07-red" if pct < 0 else "ak07-gray")
    return f'<div class="ak07-block {css}">{symbol}<br>{pct:+.2f}%</div>'


def fmt(value: float | int | None, decimals: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{decimals}f}" if isinstance(value, float) else f"{value:,}"


def render_s1_oi_strategy_panel(code: str) -> None:
    """Strategy 1 — AK07 OI levels / position for one index."""
    cfg = INDEX_CONFIGS[code]
    state = cache_manager.get_json(cache_manager.INDEX_STATE_KEY_TEMPLATE.format(index=code))
    if not state:
        st.info(f"No live state for {cfg.display} yet — is the engine running?")
        return

    spot = state.get("spot")
    call_wall = state.get("call_wall")
    put_floor = state.get("put_floor")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Live Spot Price", fmt(float(spot)) if spot is not None else "—")
    c2.metric(
        "Institutional Call Wall \u2191",
        fmt(call_wall, 0),
        delta=(f"{call_wall - spot:+,.0f} pts away" if call_wall and spot else None),
        delta_color="off",
    )
    c3.metric(
        "Institutional Put Floor \u2193",
        fmt(put_floor, 0),
        delta=(f"{spot - put_floor:+,.0f} pts above" if put_floor and spot else None),
        delta_color="off",
    )
    with c4:
        st.markdown("**Component Bias**")
        st.markdown(bias_badge(state.get("component_bias", "NEUTRAL")), unsafe_allow_html=True)
        st.caption(f"Trades today: {state.get('trades_today', 0)}/{state.get('max_trades', 2)}")

    st.markdown("#### Institutional Component Health")
    components: dict[str, float | None] = state.get("components") or {}
    if components:
        cols = st.columns(len(components))
        for col, (symbol, pct) in zip(cols, components.items()):
            col.markdown(component_block(symbol, pct), unsafe_allow_html=True)
    else:
        st.caption("Component quotes unavailable.")

    position = state.get("position")
    st.markdown("#### Active Position")
    if position:
        p1, p2, p3, p4, p5, p6 = st.columns(6)
        option = f"{position.get('option_strike', '')}{position.get('option_type', '')}"
        p1.metric("Direction", position.get("direction", "—"), delta=option or None, delta_color="off")
        p2.metric("Entry (spot)", fmt(float(position.get("entry_price", 0))))
        p3.metric("Target", fmt(float(position.get("target_price", 0))))
        p4.metric("Stop-Loss", fmt(float(position.get("sl_price", 0))))
        lots = int(position.get("lots_remaining", 1))
        _, partial_pts, _ = INDEX_OI_RISK.get(code, DEFAULT_OI_RISK)
        partial_label = (
            "1 lot booked"
            if position.get("partial_booked")
            else f"awaiting +{int(partial_pts)} book"
        )
        p5.metric(
            "Lots Running",
            f"{lots}/2",
            delta=partial_label,
            delta_color="off",
        )
        if spot is not None:
            live_pnl = (
                spot - position["entry_price"]
                if position.get("direction") == "LONG"
                else position["entry_price"] - spot
            )
            p6.metric("Live P&L (pts)", f"{live_pnl:+.2f}")
    else:
        st.caption("Flat — no open position.")

    recent = state.get("recent_trades") or []
    if recent:
        with st.expander("Strategy 1 — today's trade log", expanded=False):
            for line in reversed(recent):
                st.markdown(f'<p class="ak07-signal-line">{line}</p>', unsafe_allow_html=True)

    updated = str(state.get("updated_at", ""))[:19].replace("T", " ")
    flags = []
    if state.get("entries_blocked"):
        flags.append("entries blocked")
    elif state.get("monitoring_active"):
        flags.append("monitoring active execution boundaries")
    if state.get("paper_trading"):
        flags.append("paper mode")
    st.caption(f"Engine state updated {updated}" + (f" · {' · '.join(flags)}" if flags else ""))


def render_smc_crt_strategy_panel(symbol_code: str, *, show_header: bool = True) -> None:
    """Strategy Type 2 — SMC+CRT block."""
    from app.ui.styles import strategy_card_header

    if show_header:
        st.markdown(
            strategy_card_header("S2 · SMC + CRT", f"{symbol_code} · CRT / FVG / 1 lot options"),
            unsafe_allow_html=True,
        )

    smc = cache_manager.get_json(cache_manager.SMC_CRT_STATE_KEY_TEMPLATE.format(symbol=symbol_code))
    smc_hb = cache_manager.get_json(cache_manager.SMC_CRT_HEARTBEAT_KEY)

    if not smc and not smc_hb:
        st.caption("SMC+CRT engine offline — start `smc_crt_engine` service or MOCK mode.")
        return

    if smc_hb:
        end = smc_hb.get("session_end_ist", "15:30")
        mode = "PAPER" if smc_hb.get("paper_trading") else "LIVE"
        st.caption(f"SMC engine heartbeat {str(smc_hb.get('at', ''))[:19]} · until {end} IST · {mode} · 1 lot options")

    if not smc:
        active = (smc_hb or {}).get("instruments") or []
        if active and symbol_code not in active:
            st.info(
                f"S2 SMC+CRT is **not enabled** on {symbol_code} — "
                f"active: **{', '.join(active)}** only (3yr backtest)."
            )
        else:
            st.info(f"No SMC state for {symbol_code} yet — waiting for CRT / market data.")
        return

    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("CRH (range high)", fmt(smc.get("crh")))
    s2.metric("CRM (equilibrium)", fmt(smc.get("crm")))
    s3.metric("CRL (range low)", fmt(smc.get("crl")))
    s4.metric("Spot", fmt(float(smc["spot"])) if smc.get("spot") is not None else "—")
    setup = str(smc.get("setup_label") or "—")
    s5.metric("Setup", setup[:22] + "…" if len(setup) > 22 else setup)
    if len(setup) > 22:
        st.markdown(f'<p class="ak07-muted-line">Setup detail: {setup}</p>', unsafe_allow_html=True)

    flags = []
    if smc.get("swept_low"):
        flags.append("CRL swept")
    if smc.get("swept_high"):
        flags.append("CRH swept")
    if smc.get("crt_ready"):
        flags.append("CRT locked")
    if smc.get("entries_blocked"):
        flags.append("entries blocked")
    if flags:
        st.markdown(f'<p class="ak07-muted-line">{" · ".join(flags)}</p>', unsafe_allow_html=True)

    fvg = smc.get("fvg") or {}
    if fvg:
        fvg_line = (
            f"Last FVG: {fvg.get('direction', '—')} "
            f"{fmt(float(fvg.get('low'))) if fvg.get('low') is not None else '—'} – "
            f"{fmt(float(fvg.get('high'))) if fvg.get('high') is not None else '—'}"
        )
        st.markdown(f'<p class="ak07-muted-line">{fvg_line}</p>', unsafe_allow_html=True)

    st.markdown("##### Strategy Type 2 — Active Position")
    smc_pos = smc.get("position")
    if smc_pos:
        p1, p2, p3, p4, p5, p6 = st.columns(6)
        option = f"{smc_pos.get('option_strike', '')}{smc_pos.get('option_type', '')}"
        p1.metric("Direction", smc_pos.get("direction", "—"), delta=option or None, delta_color="off")
        p2.metric("Entry", fmt(float(smc_pos.get("entry_price", 0))))
        p3.metric("Stop (FVG)", fmt(float(smc_pos.get("sl_price", 0))))
        p4.metric("TP1 (1R book)", fmt(float(smc_pos.get("tp1_price", 0))))
        p5.metric("TP2 (2R)", fmt(float(smc_pos.get("tp2_price", 0))))
        if smc.get("spot") is not None and smc_pos.get("entry_price") is not None:
            spot = float(smc["spot"])
            entry = float(smc_pos["entry_price"])
            pnl = spot - entry if smc_pos.get("direction") == "LONG" else entry - spot
            p6.metric("Live P&L (pts)", f"{pnl:+.2f}")
    else:
        st.caption("Flat — waiting for sweep + FVG + confirmation (1 lot · Nifty options).")

    signals = smc.get("signals") or []
    if signals:
        with st.expander("Recent SMC signals", expanded=False):
            for line in signals:
                st.markdown(f'<p class="ak07-signal-line">{line}</p>', unsafe_allow_html=True)

    updated = str(smc.get("updated_at", ""))[:19].replace("T", " ")
    st.markdown(
        f'<p class="ak07-muted-line">SMC state updated {updated} · trades today {smc.get("trades_today", 0)}</p>',
        unsafe_allow_html=True,
    )


def render_s7_strategy_panel(index_code: str, *, show_header: bool = True) -> None:
    """Strategy 7 — ORB+ ADX block."""
    from app.ui.styles import strategy_card_header

    if show_header:
        st.markdown(
            strategy_card_header("S7 · ORB+ ADX", f"{index_code} · opening range breakout"),
            unsafe_allow_html=True,
        )

    s7 = cache_manager.get_json(cache_manager.S7_STATE_KEY)

    if not s7:
        st.caption("S7 engine offline — is the `engine` service running?")
        return

    updated = str(s7.get("timestamp", ""))[:19].replace("T", " ")
    upstox = cache_manager.get_json(cache_manager.UPSTOX_DAILY_PNL_KEY) or {}
    upstox_total = upstox.get("total_pnl_inr")
    if upstox_total is not None:
        pnl_str = f"₹{float(upstox_total):+,.0f} (Upstox)"
    else:
        total_pnl = s7.get("total_daily_pnl_inr")
        pnl_str = f"₹{total_pnl:+,.0f}" if total_pnl is not None else "—"
    st.caption(f"S7 state updated {updated} · day P&L {pnl_str}")

    indices: dict = s7.get("indices") or {}
    idx = indices.get(index_code)

    if not idx:
        st.info(f"No S7 state for {index_code} yet — waiting for market open.")
        return

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Spot", fmt(float(idx["spot"])) if idx.get("spot") is not None else "—")
    c2.metric("OR High", fmt(idx.get("or_high")))
    c3.metric("OR Low", fmt(idx.get("or_low")))
    c4.metric("Day Review", str(idx.get("day_review") or "PENDING"))
    c5.metric("Trades Today", f"{idx.get('trades_today', 0)}/{2}")

    setup = str(idx.get("setup_label") or "—")
    st.markdown(f'<p class="ak07-muted-line">{setup}</p>', unsafe_allow_html=True)

    st.markdown("##### Strategy 7 — Active Position")
    pos = idx.get("position")
    if pos:
        p1, p2, p3, p4, p5 = st.columns(5)
        p1.metric("Direction", pos.get("direction", "—"))
        p2.metric("Entry", fmt(float(pos.get("entry", 0))))
        p3.metric("Stop-Loss", fmt(float(pos.get("sl", 0))))
        p4.metric("TP1", fmt(float(pos.get("tp1", 0))))
        p5.metric("Lots", str(pos.get("lots", 1)))
        if idx.get("spot") is not None and pos.get("entry") is not None:
            live_pnl = (
                float(idx["spot"]) - float(pos["entry"])
                if pos.get("direction") == "LONG"
                else float(pos["entry"]) - float(idx["spot"])
            )
            st.metric("Live P&L (pts)", f"{live_pnl:+.2f}")
    else:
        st.caption("Flat — waiting for ORB breakout + ADX confirmation.")

    signals = idx.get("signals") or []
    if signals:
        with st.expander("Recent S7 signals", expanded=False):
            for line in reversed(signals):
                st.markdown(f'<p class="ak07-signal-line">{line}</p>', unsafe_allow_html=True)


def render_choch_strategy_panel(index_code: str) -> None:
    """Strategy 8 — CHOCH reversal block."""
    from app.ui.styles import strategy_card_header

    st.markdown(
        strategy_card_header("S8 · CHOCH", f"{index_code} · change of character"),
        unsafe_allow_html=True,
    )

    choch = cache_manager.get_json(cache_manager.CHOCH_STATE_KEY)

    if not choch:
        st.caption("CHOCH engine offline — start `choch_engine` service.")
        return

    updated = str(choch.get("timestamp", ""))[:19].replace("T", " ")
    upstox = cache_manager.get_json(cache_manager.UPSTOX_DAILY_PNL_KEY) or {}
    upstox_total = upstox.get("total_pnl_inr")
    if upstox_total is not None:
        pnl_str = f"\u20b9{float(upstox_total):+,.0f} (Upstox)"
    else:
        total_pnl = choch.get("total_daily_pnl_inr")
        pnl_str = f"\u20b9{total_pnl:+,.0f}" if total_pnl is not None else "\u2014"
    st.caption(f"CHOCH state updated {updated} \u00b7 day P&L {pnl_str}")

    indices: dict = choch.get("indices") or {}
    idx = indices.get(index_code)

    if not idx:
        st.info(f"No CHOCH state for {index_code} yet — waiting for market open.")
        return

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Spot", fmt(float(idx["spot"])) if idx.get("spot") is not None else "\u2014")
    c2.metric("Structure", str(idx.get("structure") or "NEUTRAL"))
    c3.metric("Last SH", fmt(idx.get("last_sh")))
    c4.metric("Last SL", fmt(idx.get("last_sl")))
    c5.metric("Trades Today", f"{idx.get('trades_today', 0)}/2")

    ss = idx.get("structure_state") or {}
    if ss.get("prev_sh") is not None or ss.get("prev_sl") is not None:
        st.caption(
            f"Prev SH {fmt(ss.get('prev_sh'))} · Prev SL {fmt(ss.get('prev_sl'))}"
            + (f" · CHOCH pending {ss.get('choch_pending')} @ {fmt(ss.get('bos_level'))}"
               if ss.get("choch_pending") else "")
        )

    setup = str(idx.get("setup_label") or "\u2014")
    st.markdown(f'<p class="ak07-muted-line">{setup}</p>', unsafe_allow_html=True)

    pos = idx.get("position")
    if pos:
        st.markdown("##### CHOCH \u2014 Active Position")
        p1, p2, p3, p4, p5 = st.columns(5)
        p1.metric("Direction", pos.get("direction", "\u2014"))
        p2.metric("Entry", fmt(float(pos.get("entry", 0))))
        p3.metric("Stop-Loss", fmt(float(pos.get("sl", 0))))
        p4.metric("Target", fmt(float(pos.get("tp", 0))))
        p5.metric("Lots", str(pos.get("lots", 1)))
        if idx.get("spot") is not None and pos.get("entry") is not None:
            live_pnl = (
                float(idx["spot"]) - float(pos["entry"])
                if pos.get("direction") == "LONG"
                else float(pos["entry"]) - float(idx["spot"])
            )
            st.metric("Live P&L (pts)", f"{live_pnl:+.2f}")
    else:
        st.caption("Flat \u2014 watching for CHOCH + ADX + 1H alignment.")

    signals = idx.get("signals") or []
    if signals:
        with st.expander("Recent CHOCH signals", expanded=False):
            for line in reversed(signals):
                st.markdown(f'<p class="ak07-signal-line">{line}</p>', unsafe_allow_html=True)

    rejected = idx.get("rejected_signals") or []
    if rejected:
        with st.expander("Rejected CHOCH signals (triggered but not traded)", expanded=False):
            for row in reversed(rejected):
                if not isinstance(row, dict):
                    continue
                at = str(row.get("at", ""))[:16].replace("T", " ")
                st.markdown(
                    f'<p class="ak07-signal-line">'
                    f'{at} · {row.get("signal_type", "?")} {row.get("direction", "?")} '
                    f'@ {fmt(row.get("signal_level"))} · {row.get("reason", "?")}'
                    f'</p>',
                    unsafe_allow_html=True,
                )


def render_gamma_expiry_panel(index_code: str, *, show_header: bool = True) -> None:
    """Gamma Expiry Observer — paper hero-zero signals on expiry days only."""
    from app.ui.styles import strategy_card_header

    if show_header:
        st.markdown(
            strategy_card_header(
                "Gamma · Expiry Observer",
                f"{index_code} · paper · observer only",
            ),
            unsafe_allow_html=True,
        )

    gamma = cache_manager.get_json(cache_manager.GAMMA_STATE_KEY)
    hb = cache_manager.get_json(cache_manager.GAMMA_HEARTBEAT_KEY)

    if not gamma and not hb:
        # Parent section may already show a single offline line; avoid a second ghost caption.
        if show_header:
            st.caption("Gamma observer offline — start `gamma_expiry_engine` service.")
        return

    if hb:
        exp_today = hb.get("expiry_today") or []
        mode = "PAPER · no orders"
        st.caption(
            f"Heartbeat {str(hb.get('at', ''))[:19]} · {mode} · "
            f"expiry today: {', '.join(exp_today) if exp_today else 'none'}"
        )

    indices: dict = (gamma or {}).get("indices") or {}
    idx = indices.get(index_code) or {}

    if not idx.get("is_expiry"):
        rule = {"NIFTY": "Tue weekly", "BANKNIFTY": "last Thu monthly", "SENSEX": "Thu weekly"}.get(
            index_code, ""
        )
        st.info(f"Not an expiry session for {index_code} ({rule}). Observer idle.")
        bt = (gamma or {}).get("backtest_refined") or cache_manager.get_json(cache_manager.GAMMA_BACKTEST_KEY)
        if bt and isinstance(bt, dict) and bt.get("refined_config"):
            rc = bt["refined_config"]
            st.caption(
                f"Backtest refined: pin≤{rc.get('pin_distance_pct')}% · "
                f"IDR≥{rc.get('min_idr_pct')}% · score≥{rc.get('min_blast_score', 55)}"
            )
        return

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Spot", fmt(float(idx["spot"])) if idx.get("spot") is not None else "—")
    c2.metric("Pin strike", str(idx.get("pin_strike") or "—"))
    c3.metric("Pin dist", f"{idx.get('pin_distance_pct', 0):.2f}%")
    c4.metric("Blast score", str(idx.get("blast_score") or "—"))
    c5.metric("Regime", str(idx.get("regime") or "—"))
    c6.metric("Signal", str(idx.get("observer_signal") or "—"))

    st.markdown(f"**Expiry:** {idx.get('expiry_date', '—')} · {idx.get('expiry_rule', '')}")
    st.markdown(f"**Call wall / Put floor:** {idx.get('call_wall')} / {idx.get('put_floor')}")
    st.markdown(f"**IDR:** {idx.get('idr_pct', 0):.2f}% · **PCR:** {idx.get('pcr_oi', '—')} · **Gamma flip:** {idx.get('gamma_flip') or '—'}")

    blast_on = idx.get("blast_window_active")
    if blast_on:
        st.success(f"Blast window ACTIVE ({idx.get('blast_window', '13:30-15:00')})")
    else:
        st.warning(f"Pre/post blast window ({idx.get('blast_window', '13:30-15:00 IST')})")

    st.markdown(f'<p class="ak07-muted-line">{idx.get("setup_label") or idx.get("observer_detail") or "—"}</p>', unsafe_allow_html=True)

    hero = idx.get("paper_hero")
    if hero:
        st.markdown("##### Paper hero signal (not executed)")
        h1, h2, h3, h4 = st.columns(4)
        h1.metric("Strike", f"{hero.get('strike')} {hero.get('option_type', '')}")
        h2.metric("Entry prem", f"{hero.get('entry_premium', '—')}")
        h3.metric("Target prem", f"{hero.get('tp_premium', '—')}")
        h4.metric("SL prem", f"{hero.get('sl_premium', '—')}")
        st.caption(
            f"Spot @ signal {hero.get('spot_at_signal', '—')} · pin {hero.get('pin_strike', '—')} · "
            f"source {hero.get('premium_source', '—')}"
        )

    last_log = idx.get("last_paper_log")
    if last_log and isinstance(last_log, dict):
        with st.expander("Last paper signal log row", expanded=False):
            st.json(last_log)

    signals = idx.get("signals") or []
    if signals:
        with st.expander("Gamma observer log (paper)", expanded=True):
            for line in reversed(signals):
                st.markdown(f'<p class="ak07-signal-line">{line}</p>', unsafe_allow_html=True)


def render_breakout_strategy_panel(index_code: str) -> None:
    """Strategy Type 3 — BLR Breakout block (Nifty only)."""
    from app.ui.styles import checklist_pills, strategy_card_header

    st.markdown(
        strategy_card_header(
            "S3 · BLR Breakout",
            "Nifty options · shared strike fan-out · trail after +10 · max 3 trades/day",
        ),
        unsafe_allow_html=True,
    )

    bo = cache_manager.get_json(cache_manager.BREAKOUT_STATE_KEY_TEMPLATE.format(index=index_code))
    bo_hb = cache_manager.get_json(cache_manager.BREAKOUT_HEARTBEAT_KEY)

    entries_enabled = None
    if bo:
        entries_enabled = bo.get("entries_enabled")
    elif bo_hb:
        entries_enabled = bo_hb.get("entries_enabled")
    if entries_enabled is False or (
        bo and bo.get("entries_blocked") and "disabled" in str(bo.get("block_reason", "")).lower()
    ):
        st.warning(
            "S3 trading **disabled** (3-year backtest: no edge). "
            "Engine publishes BLR levels + Day Review for S2 only."
        )

    if not bo and not bo_hb:
        st.info(
            "Breakout engine offline.\n\n"
            "1. Start `breakout_engine` (or enable MOCK)\n"
            "2. Complete **Token Update** for your broker\n"
            "3. Wait for session open / Day Review"
        )
        return

    if bo_hb:
        end = bo_hb.get("session_end_ist", "15:30")
        mode = "PAPER" if bo_hb.get("paper_trading") else "LIVE"
        engine_ok = True
        st.markdown(
            checklist_pills(
                [
                    ("Engine online", "ok" if engine_ok else "bad"),
                    (f"Session → {end} IST", "ok"),
                    (mode, "warn" if mode == "PAPER" else "ok"),
                ]
            ),
            unsafe_allow_html=True,
        )

    if not bo:
        st.info(
            f"No live breakout state for {index_code} yet.\n\n"
            "Levels appear after the 9:15 open freezes. "
            "Meanwhile keep Token Update connected."
        )
        return

    b1, b2, b3, b4, b5 = st.columns(5)
    b1.metric("Green (buy line)", fmt(bo.get("green")))
    b2.metric("Mid (9:15 open)", fmt(bo.get("mid")))
    b3.metric("Red (sell line)", fmt(bo.get("red")))
    b4.metric("Spot", fmt(float(bo["spot"])) if bo.get("spot") is not None else "—")
    review = str(bo.get("day_review") or "PENDING")
    b5.metric("Day review", review)

    profile = auth_session.current_profile()
    username = auth_session.current_username()
    user_broker = str(profile.get("broker") or "upstox").strip().lower()
    if user_broker == "groww" and username:
        pnl_snap = refresh_groww_pnl_if_stale(username)
    else:
        pnl_snap = get_user_broker_pnl(username, user_broker)
    pnl_total = pnl_snap.get("total_pnl_inr")
    pnl_realised = pnl_snap.get("realised_inr")
    pnl_updated = str(pnl_snap.get("updated_at") or "")[:19].replace("T", " ")
    d1, d2, d3 = st.columns(3)
    d1.metric(broker_pnl_label(user_broker), format_pnl_inr(float(pnl_total) if pnl_total is not None else None))
    d2.metric("Realised today", format_pnl_inr(float(pnl_realised) if pnl_realised is not None else None))
    d3.metric("Trades today", f"{bo.get('trades_today', 0)}/{bo.get('max_trades', 3)}")

    meta = []
    if bo.get("session_open") is not None:
        src = str(bo.get("session_open_source") or "")
        src_label = {
            "candle": "candle",
            "day_ohlc": "NSE day",
            "ltp_provisional": "LTP*",
        }.get(src, src)
        open_txt = fmt(float(bo["session_open"]))
        broker_session_open = bo.get("broker_session_open")
        if broker_session_open is not None and abs(float(broker_session_open) - float(bo["session_open"])) >= 0.01:
            open_txt = f"{open_txt} (5m open {fmt(float(broker_session_open))})"
        meta.append(f"9:15 open {open_txt} ({src_label})")
    if bo.get("prev_close") is not None:
        meta.append(f"prev close {fmt(float(bo['prev_close']))}")
    if bo.get("gap_regime"):
        meta.append(str(bo["gap_regime"]))
    if bo.get("band_half_pct") is not None:
        meta.append(f"band {float(bo['band_half_pct']):.3f}% half")
    if bo.get("first_candle_close") is not None and bo.get("mid") is not None:
        meta.append(f"1st 5m close {fmt(float(bo['first_candle_close']))} vs mid")
    if bo.get("entries_blocked"):
        meta.append("entries blocked")
    if meta:
        st.markdown(f'<p class="ak07-muted-line">{" · ".join(meta)}</p>', unsafe_allow_html=True)

    setup = str(bo.get("setup_label") or "—")
    st.markdown(f'<p class="ak07-muted-line">{setup}</p>', unsafe_allow_html=True)

    st.markdown("##### Deployed position")
    bo_pos = bo.get("position")
    if bo_pos:
        p1, p2, p3, p4, p5, p6 = st.columns(6)
        contract = str(bo_pos.get("contract_label") or "").strip()
        if not contract:
            strike = bo_pos.get("option_strike", "")
            ot = bo_pos.get("option_type", "")
            contract = f"{strike}{ot}" if strike and ot else ""
        p1.metric("Direction", bo_pos.get("direction", "—"), delta=contract or None, delta_color="off")
        entry = float(bo_pos.get("entry_price", 0))
        p2.metric("Entry (spot)", fmt(entry))
        tp_pts = bo.get("tp1_points")
        tp_label = f"TP1 ({int(tp_pts)}pt)" if tp_pts else "TP1"
        p3.metric(tp_label, fmt(float(bo_pos.get("tp1_price", 0))))
        p4.metric("Stop-Loss", fmt(float(bo_pos.get("sl_price", 0))))
        spot = bo.get("spot")
        if spot is not None:
            live_pnl = (
                float(spot) - entry
                if bo_pos.get("direction") == "LONG"
                else entry - float(spot)
            )
            p5.metric("Live P&L (pts)", f"{live_pnl:+.2f}")
        else:
            p5.metric("Live P&L (pts)", "—")
        p6.metric("Reason", str(bo_pos.get("entry_reason", "—"))[:18])
        legs = bo_pos.get("order_legs") or []
        if legs:
            with st.expander("Broker execution legs", expanded=True):
                for leg in legs:
                    if not isinstance(leg, dict):
                        continue
                    user = leg.get("username", "?")
                    broker = leg.get("broker", "?")
                    oid = leg.get("groww_order_id") or leg.get("upstox_order_id") or "—"
                    sym = leg.get("contract_label") or leg.get("trading_symbol") or leg.get("groww_symbol") or ""
                    st.markdown(
                        f"**{user}** @{broker} · {sym} · order `{oid}`"
                    )
        st.caption(str(bo_pos.get("entry_reason", "")))
    else:
        st.info(
            "**No deployed S3 position** — flat right now.\n\n"
            f"When a breakout fires you will see strike, SL/TP, live P&L, and broker legs here "
            f"(max {bo.get('max_trades', 3)} trades/day). "
            "Closed trades land under **Performance Review**."
        )

    signals = bo.get("signals") or []
    if signals:
        with st.expander("Recent breakout signals", expanded=False):
            for line in reversed(signals[-8:]):
                st.markdown(f'<p class="ak07-signal-line">{line}</p>', unsafe_allow_html=True)

    updated = str(bo.get("updated_at", ""))[:19].replace("T", " ")
    st.markdown(
        f'<p class="ak07-muted-line">Breakout state updated {updated} · trades today '
        f'{bo.get("trades_today", 0)}/{bo.get("max_trades", 3)} · '
        f'{broker_pnl_label(user_broker)} updated {pnl_updated or "—"}</p>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Layout: slim sidebar + top status bar (full-width main content)
# ---------------------------------------------------------------------------
def _render_strategy_sections() -> None:
    """Stacked strategy cards. Kept separate so a fragment can refresh without full-page blink."""
    from app.ui.styles import strategy_card_header

    tabbed_codes = tabbed_dashboard_index_codes()

    if user_can_view_strategy(STRATEGY_S3_BREAKOUT):
        with st.container(border=True, key="ak07_s3"):
            for s3_code in S3_BREAKOUT_INDICES:
                render_breakout_strategy_panel(s3_code)

    if user_can_view_strategy(STRATEGY_S1_OI) and tabbed_codes:
        with st.container(border=True, key="ak07_s1"):
            st.markdown(
                strategy_card_header(
                    "S1 · AK07 OI",
                    "Institutional walls · component bias · max 2 trades/day",
                ),
                unsafe_allow_html=True,
            )
            if len(tabbed_codes) == 1:
                render_s1_oi_strategy_panel(tabbed_codes[0])
            else:
                s1_tabs = st.tabs([INDEX_CONFIGS[c].display for c in tabbed_codes])
                for tab, code in zip(s1_tabs, tabbed_codes):
                    with tab:
                        render_s1_oi_strategy_panel(code)

    if user_can_view_strategy(STRATEGY_S2_SMC) and tabbed_codes:
        with st.container(border=True, key="ak07_s2"):
            if len(tabbed_codes) == 1:
                render_smc_crt_strategy_panel(tabbed_codes[0])
            else:
                st.markdown(
                    strategy_card_header("S2 · SMC + CRT", "CRT / FVG · 1 lot options"),
                    unsafe_allow_html=True,
                )
                s2_tabs = st.tabs([INDEX_CONFIGS[c].display for c in tabbed_codes])
                for tab, code in zip(s2_tabs, tabbed_codes):
                    with tab:
                        render_smc_crt_strategy_panel(code, show_header=False)

    if user_can_view_strategy(STRATEGY_S7_ORB) and tabbed_codes:
        with st.container(border=True, key="ak07_s7"):
            if len(tabbed_codes) == 1:
                render_s7_strategy_panel(tabbed_codes[0])
            else:
                st.markdown(
                    strategy_card_header("S7 · ORB+ ADX", "Opening range breakout"),
                    unsafe_allow_html=True,
                )
                s7_tabs = st.tabs([INDEX_CONFIGS[c].display for c in tabbed_codes])
                for tab, code in zip(s7_tabs, tabbed_codes):
                    with tab:
                        render_s7_strategy_panel(code, show_header=False)

    if user_can_view_strategy(STRATEGY_GAMMA) and tabbed_codes:
        with st.container(border=True, key="ak07_gamma"):
            from app.ui.styles import strategy_card_header as _hdr

            st.markdown(
                _hdr("Gamma · Expiry Observer", "Paper · observer only"),
                unsafe_allow_html=True,
            )
            gamma = cache_manager.get_json(cache_manager.GAMMA_STATE_KEY)
            hb = cache_manager.get_json(cache_manager.GAMMA_HEARTBEAT_KEY)
            # One offline line for the whole section (not once per index tab — that caused blink/ghost text).
            if not gamma and not hb:
                st.caption("Gamma observer offline — start `gamma_expiry_engine` service.")
            elif len(tabbed_codes) == 1:
                render_gamma_expiry_panel(tabbed_codes[0], show_header=False)
            else:
                g_tabs = st.tabs([INDEX_CONFIGS[c].display for c in tabbed_codes])
                for tab, code in zip(g_tabs, tabbed_codes):
                    with tab:
                        render_gamma_expiry_panel(code, show_header=False)


def run_dashboard() -> None:
    auth_session.require_login()
    profile = auth_session.current_profile()
    render_compact_sidebar(mock_mode=MOCK_MODE, admin_mode=auth_session.is_admin())

    # Available capital / today's P&L — Dashboard only (not on other pages).
    render_funds_summary(
        username=auth_session.current_username(),
        broker=str(profile.get("broker") or "upstox"),
    )

    # ---------------------------------------------------------------------------
    # Dashboard overview (title + status pills) — one card
    # ---------------------------------------------------------------------------
    with st.container(border=True, key="ak07_dash_home"):
        st.markdown('<p class="ak07-dash-title">Dashboard</p>', unsafe_allow_html=True)
        st.markdown(
            f'<p class="ak07-dash-sub">Live levels · broker P&L · deploy status · '
            f"updated {datetime.now(timezone.utc).astimezone().strftime('%H:%M:%S')} · "
            f"{enabled_strategy_labels_text()}</p>",
            unsafe_allow_html=True,
        )
        auto_refresh = render_top_status_bar(
            mock_mode=MOCK_MODE,
            production_domain=PRODUCTION_DOMAIN,
            refresh_seconds=REFRESH_SECONDS,
            can_view_strategy=user_can_view_strategy,
            username=auth_session.current_username(),
            broker=str(profile.get("broker") or "upstox"),
        )

    # Fragment refresh avoids full-page sleep+rerun blink (ghost duplicate text under Gamma).
    interval = timedelta(seconds=REFRESH_SECONDS) if auto_refresh else None

    @st.fragment(run_every=interval)
    def _live_strategy_panels() -> None:
        _render_strategy_sections()

    _live_strategy_panels()


st.set_page_config(
    page_title="AK07 Execution Cockpit",
    page_icon="\U0001f3af",
    layout="wide",
    # auto: expanded on desktop, collapsed drawer on phones
    initial_sidebar_state="auto",
)

inject_dark_theme()
build_navigation(dashboard_runner=run_dashboard).run()
