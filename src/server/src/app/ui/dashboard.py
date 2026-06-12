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
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services import cache_manager
from app.services.upstox_engine import INDEX_CONFIGS
from app.ui.cockpit_layout import render_compact_sidebar, render_top_status_bar
from app.ui.styles import inject_dark_theme

REFRESH_SECONDS = 5
MOCK_MODE = os.environ.get("AK07_MOCK") == "1"
PRODUCTION_DOMAIN = (os.environ.get("PRODUCTION_DOMAIN") or "").strip() or "ak07.in"

if MOCK_MODE:
    from app.services import mock_data

    mock_data.seed()

st.set_page_config(
    page_title="AK07 Execution Cockpit",
    page_icon="\U0001f3af",
    layout="wide",
    initial_sidebar_state="collapsed",
)

inject_dark_theme()


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


def render_smc_crt_strategy_panel(symbol_code: str) -> None:
    """Strategy Type 2 — SMC+CRT block (below AK07 Active Position)."""
    st.markdown("---")
    st.markdown("#### Strategy Type 2 — SMC + CRT")

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
        st.info(f"No SMC state for {symbol_code} yet.")
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


def render_breakout_strategy_panel(index_code: str) -> None:
    """Strategy Type 3 — BLR Breakout block."""
    st.markdown("---")
    st.markdown("#### Strategy Type 3 — BLR Breakout")

    bo = cache_manager.get_json(cache_manager.BREAKOUT_STATE_KEY_TEMPLATE.format(index=index_code))
    bo_hb = cache_manager.get_json(cache_manager.BREAKOUT_HEARTBEAT_KEY)

    if not bo and not bo_hb:
        st.caption("Breakout engine offline — start `breakout_engine` service or MOCK mode.")
        return

    if bo_hb:
        end = bo_hb.get("session_end_ist", "15:30")
        mode = "PAPER" if bo_hb.get("paper_trading") else "LIVE"
        st.caption(
            f"Breakout heartbeat {str(bo_hb.get('at', ''))[:19]} · session until {end} IST · {mode}"
        )

    if not bo:
        st.info(f"No breakout state for {index_code} yet.")
        return

    b1, b2, b3, b4, b5 = st.columns(5)
    b1.metric("Green (buy line)", fmt(bo.get("green")))
    b2.metric("Mid (pivot)", fmt(bo.get("mid")))
    b3.metric("Red (sell line)", fmt(bo.get("red")))
    b4.metric("Spot", fmt(float(bo["spot"])) if bo.get("spot") is not None else "—")
    review = str(bo.get("day_review") or "PENDING")
    b5.metric("Day review", review)

    meta = []
    if bo.get("gap_regime"):
        meta.append(str(bo["gap_regime"]))
    if bo.get("first_candle_close") is not None and bo.get("mid") is not None:
        meta.append(f"1st 5m close {fmt(float(bo['first_candle_close']))} vs mid")
    if bo.get("entries_blocked"):
        meta.append("entries blocked")
    if meta:
        st.markdown(f'<p class="ak07-muted-line">{" · ".join(meta)}</p>', unsafe_allow_html=True)

    setup = str(bo.get("setup_label") or "—")
    st.markdown(f'<p class="ak07-muted-line">{setup}</p>', unsafe_allow_html=True)

    st.markdown("##### Strategy Type 3 — Active Position")
    bo_pos = bo.get("position")
    if bo_pos:
        p1, p2, p3, p4, p5, p6 = st.columns(6)
        option = f"{bo_pos.get('option_strike', '')}{bo_pos.get('option_type', '')}"
        p1.metric("Direction", bo_pos.get("direction", "—"), delta=option or None, delta_color="off")
        p2.metric("Entry (spot)", fmt(float(bo_pos.get("entry_price", 0))))
        p3.metric("TP1 (1R book)", fmt(float(bo_pos.get("tp1_price", 0))))
        p4.metric("TP2 (2R)", fmt(float(bo_pos.get("tp2_price", 0))))
        p5.metric("Stop-Loss", fmt(float(bo_pos.get("sl_price", 0))))
        p6.metric("Reason", str(bo_pos.get("entry_reason", "—"))[:18])
    else:
        st.caption("Flat — no breakout position (1 lot · options · max 2/day).")

    signals = bo.get("signals") or []
    if signals:
        with st.expander("Recent breakout signals", expanded=False):
            for line in reversed(signals[-8:]):
                st.markdown(f'<p class="ak07-signal-line">{line}</p>', unsafe_allow_html=True)

    updated = str(bo.get("updated_at", ""))[:19].replace("T", " ")
    st.markdown(
        f'<p class="ak07-muted-line">Breakout state updated {updated} · trades today '
        f'{bo.get("trades_today", 0)}/{bo.get("max_trades", 2)}</p>',
        unsafe_allow_html=True,
    )


def render_price_action_panel(index_code: str) -> None:
    """Strategy Type 4 — Advanced Price Action."""
    st.markdown("---")
    st.markdown("#### Strategy Type 4 — Price Action (Advanced)")

    pa = cache_manager.get_json(cache_manager.PA_STATE_KEY_TEMPLATE.format(index=index_code))
    pa_hb = cache_manager.get_json(cache_manager.PA_HEARTBEAT_KEY)

    if not pa and not pa_hb:
        st.caption("Price Action engine offline — start `pa_engine` service.")
        return

    if pa_hb:
        mode = "PAPER" if pa_hb.get("paper_trading") else "LIVE"
        st.caption(f"PA heartbeat {str(pa_hb.get('at', ''))[:19]} · {mode} · 1 lot options")

    if not pa:
        st.info(f"No PA state for {index_code} yet.")
        return

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("OR High", fmt(pa.get("or_high")))
    c2.metric("OR Low", fmt(pa.get("or_low")))
    c3.metric("Session VWAP", fmt(float(pa["session_vwap"])) if pa.get("session_vwap") else "—")
    c4.metric("Structure", str(pa.get("structure") or "—"))
    c5.metric("Spot", fmt(float(pa["spot"])) if pa.get("spot") is not None else "—")
    st.markdown(f'<p class="ak07-muted-line">{pa.get("setup_label", "—")}</p>', unsafe_allow_html=True)

    pos = pa.get("position")
    if pos:
        p1, p2, p3, p4, p5, p6 = st.columns(6)
        opt = f"{pos.get('option_strike', '')}{pos.get('option_type', '')}"
        p1.metric("Direction", pos.get("direction", "—"), delta=opt or None, delta_color="off")
        p2.metric("Entry", fmt(float(pos.get("entry_price", 0))))
        p3.metric("TP1 (1R book)", fmt(float(pos.get("tp1_price", 0))))
        p4.metric("TP2 (2R)", fmt(float(pos.get("tp2_price", 0))))
        p5.metric("Stop", fmt(float(pos.get("sl_price", 0))))
        p6.metric("Setup", str(pos.get("entry_reason", "—"))[:16])
    else:
        st.caption("Flat — OR sweep / BOS + volume (max 2/day).")


def render_greeks_strategy_panel(index_code: str) -> None:
    """Strategy Type 5 — Advanced Greeks."""
    st.markdown("---")
    st.markdown("#### Strategy Type 5 — Greeks (Advanced)")

    gk = cache_manager.get_json(cache_manager.GREEKS_STATE_KEY_TEMPLATE.format(index=index_code))
    hb = cache_manager.get_json(cache_manager.GREEKS_HEARTBEAT_KEY)

    if not gk and not hb:
        st.caption("Greeks engine offline — start `greeks_engine` service.")
        return

    if hb:
        mode = "PAPER" if hb.get("paper_trading") else "LIVE"
        st.caption(f"Greeks heartbeat {str(hb.get('at', ''))[:19]} · {mode} · chain per index")

    if not gk:
        st.info(f"No greeks state for {index_code} yet.")
        return

    an = gk.get("analytics") or {}
    g1, g2, g3, g4, g5, g6 = st.columns(6)
    g1.metric("Gamma flip", fmt(an.get("gamma_flip")))
    g2.metric("Skew %", f"{an.get('skew_pct', 0):+.2f}" if an.get("skew_pct") is not None else "—")
    g3.metric("PCR (OI)", fmt(an.get("pcr_oi")))
    g4.metric("Net Δ-OI", fmt(an.get("net_delta_oi"), 0))
    g5.metric("Regime", str(an.get("regime") or "—"))
    g6.metric("Bias", str(an.get("bias") or "—"))
    st.markdown(f'<p class="ak07-muted-line">{gk.get("setup_label", "—")}</p>', unsafe_allow_html=True)

    pos = gk.get("position")
    if pos:
        p1, p2, p3, p4, p5, p6 = st.columns(6)
        opt = f"{pos.get('option_strike', '')}{pos.get('option_type', '')}"
        p1.metric("Direction", pos.get("direction", "—"), delta=opt or None, delta_color="off")
        p2.metric("Entry", fmt(float(pos.get("entry_price", 0))))
        p3.metric("TP1 (1R book)", fmt(float(pos.get("tp1_price", 0))))
        p4.metric("TP2 (2R)", fmt(float(pos.get("tp2_price", 0))))
        p5.metric("Stop", fmt(float(pos.get("sl_price", 0))))
        p6.metric("Reason", str(pos.get("entry_reason", "—"))[:16])
    else:
        st.caption("Flat — skew + gamma flip confluence (max 2/day).")


def render_sr_reversal_panel(index_code: str) -> None:
    """Strategy Type 6 — Intraday S/R Reversal."""
    st.markdown("---")
    st.markdown("#### Strategy Type 6 — S/R Reversal (Intraday)")

    sr = cache_manager.get_json(cache_manager.SR_REVERSAL_STATE_KEY_TEMPLATE.format(index=index_code))
    hb = cache_manager.get_json(cache_manager.SR_REVERSAL_HEARTBEAT_KEY)

    if not sr and not hb:
        st.caption("S/R Reversal engine offline — start `sr_reversal_engine` service.")
        return

    if hb:
        mode = "PAPER" if hb.get("paper_trading") else "LIVE"
        st.caption(
            f"S/R heartbeat {str(hb.get('at', ''))[:19]} · {mode} · prior 1H swings @ open · 1 lot"
        )

    if not sr:
        st.info(f"No S/R state for {index_code} yet.")
        return

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Prior 1H Swing H", fmt(sr.get("prior_swing_high")))
    c2.metric("Prior 1H Swing L", fmt(sr.get("prior_swing_low")))
    c3.metric("Today 1H Swing H", fmt(sr.get("swing_high")))
    c4.metric("Today 1H Swing L", fmt(sr.get("swing_low")))
    c5.metric("Session H/L", f"{fmt(sr.get('session_low'))} / {fmt(sr.get('session_high'))}")
    c6.metric("Spot", fmt(float(sr["spot"])) if sr.get("spot") is not None else "—")
    st.markdown(f'<p class="ak07-muted-line">{sr.get("setup_label", "—")}</p>', unsafe_allow_html=True)

    zones = sr.get("zones") or []
    if zones:
        zone_txt = " · ".join(
            f"{z.get('label', '?')} {fmt(z.get('level'))} ({z.get('kind', '')[:1]})" for z in zones[:6]
        )
        st.caption(f"Active zones: {zone_txt}")

    pos = sr.get("position")
    if pos:
        p1, p2, p3, p4, p5, p6 = st.columns(6)
        opt = f"{pos.get('option_strike', '')}{pos.get('option_type', '')}"
        p1.metric("Direction", pos.get("direction", "—"), delta=opt or None, delta_color="off")
        p2.metric("Entry", fmt(float(pos.get("entry_price", 0))))
        p3.metric("TP1 (1R book)", fmt(float(pos.get("tp1_price", 0))))
        p4.metric("TP2 (2R)", fmt(float(pos.get("tp2_price", 0))))
        p5.metric("Stop", fmt(float(pos.get("sl_price", 0))))
        p6.metric("Zone", str(pos.get("entry_reason", "—"))[:18])
    else:
        st.caption("Flat — fade rejection at intraday S/R (max 2/day · flat 14:55).")

    signals = sr.get("signals") or []
    if signals:
        with st.expander("Recent S/R signals", expanded=False):
            for line in reversed(signals[-8:]):
                st.markdown(f'<p class="ak07-signal-line">{line}</p>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Layout: slim sidebar + top status bar (full-width main content)
# ---------------------------------------------------------------------------
render_compact_sidebar(mock_mode=MOCK_MODE)
auto_refresh = render_top_status_bar(
    mock_mode=MOCK_MODE,
    production_domain=PRODUCTION_DOMAIN,
    refresh_seconds=REFRESH_SECONDS,
)

# ---------------------------------------------------------------------------
# Main: one tab per index
# ---------------------------------------------------------------------------
st.markdown("# AK07 Multi-Index Execution Cockpit")
meta_col, domain_col = st.columns([10, 2])
with meta_col:
    st.caption(
        f"Updated {datetime.now(timezone.utc).astimezone().strftime('%H:%M:%S')} local · "
        "S1 OI · S2 SMC (Nifty) · S3 BLR · S4 PA · S5 Greeks · **Performance Review** in nav · "
        "collapse sidebar « for full width"
    )
with domain_col:
    st.caption(PRODUCTION_DOMAIN)

ak07_tabs = [cfg.display for cfg in INDEX_CONFIGS.values()]
tabs = st.tabs(ak07_tabs)

for tab, (code, cfg) in zip(tabs, INDEX_CONFIGS.items()):
    with tab:
        state = cache_manager.get_json(cache_manager.INDEX_STATE_KEY_TEMPLATE.format(index=code))
        if not state:
            st.info(f"No live state for {cfg.display} yet — is the engine running?")
            continue

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
            p5.metric(
                "Lots Running",
                f"{lots}/2",
                delta="1 lot booked" if position.get("partial_booked") else "awaiting +60 book",
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

        updated = str(state.get("updated_at", ""))[:19].replace("T", " ")
        flags = []
        if state.get("entries_blocked"):
            flags.append("entries blocked")
        elif state.get("monitoring_active"):
            flags.append("monitoring active execution boundaries")
        if state.get("paper_trading"):
            flags.append("paper mode")
        st.caption(f"Engine state updated {updated}" + (f" · {' · '.join(flags)}" if flags else ""))

        if code == "NIFTY":
            render_smc_crt_strategy_panel("NIFTY")
        else:
            st.markdown("---")
            st.markdown("#### Strategy Type 2 — SMC + CRT")
            st.caption("SMC+CRT runs on **Nifty 50** only (see Nifty tab).")

        render_breakout_strategy_panel(code)
        render_price_action_panel(code)
        render_greeks_strategy_panel(code)
        render_sr_reversal_panel(code)

if auto_refresh:
    time.sleep(REFRESH_SECONDS)
    st.rerun()
