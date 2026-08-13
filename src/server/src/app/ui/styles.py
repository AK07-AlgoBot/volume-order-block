"""Shared Streamlit dark theme + full-width layout for AK07 cockpit pages."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

DARK_THEME_CSS = """
<style>
  :root {
    --ak07-bg: #0c0f14;
    --ak07-panel: #161b24;
    --ak07-border: #232b38;
  }

  .stApp { background-color: var(--ak07-bg); color: #e6e9ef; }

  /* --- Full-width main canvas (wide layout without dead side margins) --- */
  [data-testid="stAppViewContainer"],
  [data-testid="stAppViewContainer"] > section.main,
  section.main > div.block-container {
    max-width: none !important;
    width: 100% !important;
  }

  section.main > div.block-container {
    padding-top: 0.75rem !important;
    padding-bottom: 1.5rem !important;
    padding-left: 1.25rem !important;
    padding-right: 1.5rem !important;
  }

  /* Sidebar expanded: main uses remaining viewport */
  section[data-testid="stSidebar"][aria-expanded="true"] ~ section[data-testid="stMain"] > div.block-container {
    width: calc(100vw - 17.5rem) !important;
    max-width: calc(100vw - 17.5rem) !important;
  }

  /* Sidebar collapsed: stretch main edge-to-edge */
  section[data-testid="stSidebar"][aria-expanded="false"] ~ section[data-testid="stMain"] {
    margin-left: 0 !important;
  }
  section[data-testid="stSidebar"][aria-expanded="false"] ~ section[data-testid="stMain"] > div.block-container {
    width: calc(100vw - 2.5rem) !important;
    max-width: calc(100vw - 2.5rem) !important;
    padding-left: 1.5rem !important;
    padding-right: 2rem !important;
  }

  /* Slim sidebar when open */
  section[data-testid="stSidebar"] {
    background-color: #11151c;
    width: 16rem !important;
    min-width: 16rem !important;
  }
  section[data-testid="stSidebar"] > div {
    width: 16rem !important;
    min-width: 16rem !important;
  }
  section[data-testid="stSidebar"] .block-container {
    padding-top: 0.5rem !important;
    padding-left: 0.75rem !important;
    padding-right: 0.75rem !important;
  }

  /* Tabs use full row width */
  .stTabs [data-baseweb="tab-list"] {
    gap: 0.35rem;
    flex-wrap: wrap;
  }
  .stTabs [data-baseweb="tab"] {
    padding: 0.45rem 0.9rem;
    font-size: 0.9rem;
  }

  div[data-testid="stMetric"] {
    background-color: var(--ak07-panel);
    border: 1px solid var(--ak07-border);
    border-radius: 10px;
    padding: 8px 10px;
  }
  div[data-testid="stMetric"] label {
    color: #8b96a8 !important;
    font-size: 0.76rem !important;
  }
  div[data-testid="stMetricValue"] {
    color: #e6e9ef !important;
    font-size: 1.1rem !important;
  }

  .stApp, .stApp p, .stApp li, .stApp label, .stApp span { color: #e6e9ef; }
  [data-testid="stCaptionContainer"], .stCaption { color: #b8c4d4 !important; }
  .ak07-muted-line { color: #b8c4d4; font-size: 0.85rem; }
  div[data-testid="stDataFrame"] { border: 1px solid var(--ak07-border); border-radius: 8px; }

  .ak07-top-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.65rem 1rem;
    margin-bottom: 0.35rem;
  }
  .ak07-status-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem 0.75rem;
    align-items: center;
    flex: 1 1 520px;
    background: var(--ak07-panel);
    border: 1px solid var(--ak07-border);
    border-radius: 10px;
    padding: 0.5rem 0.85rem;
    font-size: 0.82rem;
  }
  .ak07-refresh-wrap {
    flex: 0 0 auto;
    min-width: 7rem;
    text-align: right;
  }
  .ak07-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.18rem 0.55rem;
    border-radius: 999px;
    font-weight: 600;
    border: 1px solid #2a3344;
    background: #12161d;
    white-space: nowrap;
  }
  .ak07-dot-on { color: #4ade80; }
  .ak07-dot-off { color: #f87171; }
  .ak07-dot-warn { color: #fbbf24; }

  .ak07-block {
    border-radius: 8px;
    padding: 10px 6px;
    text-align: center;
    font-weight: 600;
    font-size: 0.85rem;
    color: #ffffff;
    margin-bottom: 6px;
  }
  .ak07-green { background-color: #14532d; border: 1px solid #22c55e; }
  .ak07-red { background-color: #7f1d1d; border: 1px solid #ef4444; }
  .ak07-gray { background-color: #1f2937; border: 1px solid #4b5563; }
  .ak07-badge {
    display: inline-block;
    border-radius: 6px;
    padding: 4px 12px;
    font-weight: 700;
    letter-spacing: 0.05em;
  }
  .ak07-bull { background: #14532d; color: #4ade80; }
  .ak07-bear { background: #7f1d1d; color: #f87171; }
  .ak07-neutral { background: #1f2937; color: #9ca3af; }

  /* Order-history style chips (AK07 dark — not third-party branding) */
  .ak07-chip-row {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 0.65rem;
    margin: 0.35rem 0 0.85rem 0;
  }
  .ak07-chip {
    background: var(--ak07-panel);
    border: 1px solid var(--ak07-border);
    border-radius: 10px;
    padding: 0.7rem 0.85rem;
  }
  .ak07-chip .lbl {
    display: block;
    color: #8b96a8;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    margin-bottom: 0.25rem;
  }
  .ak07-chip .val {
    display: block;
    color: #e6e9ef;
    font-size: 1.25rem;
    font-weight: 700;
    line-height: 1.2;
  }
  .ak07-chip .val.win { color: #4ade80; }
  .ak07-chip .val.loss { color: #f87171; }
  .ak07-chip .val.muted { color: #9ca3af; }

  .ak07-strategy-card {
    background: var(--ak07-panel);
    border: 1px solid var(--ak07-border);
    border-radius: 12px;
    padding: 0.85rem 1rem 0.35rem 1rem;
    margin: 0.5rem 0 0.75rem 0;
  }
  .ak07-strategy-card h3 {
    margin: 0;
    font-size: 1.05rem;
    color: #e6e9ef;
    font-weight: 700;
  }
  .ak07-strategy-card .sub {
    margin: 0.2rem 0 0.55rem 0;
    color: #8b96a8;
    font-size: 0.84rem;
  }
  .ak07-broker-card {
    background: transparent;
    border: none;
    border-radius: 0;
    padding: 0;
    margin: 0 0 0.35rem 0;
  }
  .ak07-broker-card h3 {
    margin: 0 0 0.2rem 0;
    font-size: 1.08rem;
    color: #e6e9ef;
    font-weight: 700;
  }
  .ak07-broker-card .sub {
    margin: 0;
    color: #8b96a8;
    font-size: 0.84rem;
  }
  .ak07-sec-title {
    display: flex;
    align-items: flex-start;
    gap: 0.55rem;
    margin: 0 0 0.15rem 0;
  }
  .ak07-sec-title .ico {
    font-size: 1.1rem;
    line-height: 1.3;
  }
  .ak07-creds-title {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    margin: 0.15rem 0 0.2rem 0;
  }
  .ak07-creds-title h4 {
    margin: 0;
    color: #e6e9ef;
    font-size: 0.98rem;
    font-weight: 700;
  }
  .ak07-creds-title p {
    margin: 0.2rem 0 0 0;
    color: #8b96a8;
    font-size: 0.82rem;
  }
  .ak07-status-ok {
    color: #4ade80;
    font-weight: 700;
    font-size: 0.9rem;
  }
  .ak07-status-bad {
    color: #f87171;
    font-weight: 700;
    font-size: 0.9rem;
  }
  .ak07-redirect-box {
    background: #12161d;
    border: 1px solid var(--ak07-border);
    border-radius: 8px;
    padding: 0.55rem 0.75rem;
    font-family: ui-monospace, monospace;
    font-size: 0.82rem;
    color: #cbd5e1;
    word-break: break-all;
    margin: 0.35rem 0 0.5rem 0;
  }
  .ak07-hero {
    display: flex;
    align-items: flex-start;
    gap: 0.9rem;
    background: linear-gradient(135deg, #152033 0%, #12161d 70%);
    border: 1px solid var(--ak07-border);
    border-radius: 14px;
    padding: 1.15rem 1.3rem;
    margin: 0.2rem 0 0.75rem 0;
  }
  .ak07-hero .icon {
    flex: 0 0 auto;
    width: 2.6rem;
    height: 2.6rem;
    border-radius: 10px;
    background: #0369a1;
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.25rem;
  }
  .ak07-hero .eyebrow {
    color: #7dd3fc;
    font-size: 0.72rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin: 0 0 0.3rem 0;
  }
  .ak07-hero h2 {
    margin: 0;
    color: #e6e9ef;
    font-size: 1.35rem;
    font-weight: 800;
  }
  .ak07-hero p {
    margin: 0.35rem 0 0 0;
    color: #94a3b8;
    font-size: 0.9rem;
  }
  .ak07-help-banner {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.65rem;
    background: rgba(14, 116, 144, 0.18);
    border: 1px solid #0e7490;
    border-radius: 10px;
    padding: 0.65rem 0.85rem;
    margin: 0.45rem 0 0.65rem 0;
    color: #e0f2fe;
    font-size: 0.88rem;
  }
  .ak07-help-banner a {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    background: #2563eb;
    color: #fff !important;
    text-decoration: none !important;
    border-radius: 8px;
    padding: 0.4rem 0.75rem;
    font-size: 0.8rem;
    font-weight: 700;
    white-space: nowrap;
  }
  .ak07-saved-box {
    background: rgba(20, 83, 45, 0.35);
    border: 1px solid #166534;
    border-radius: 10px;
    padding: 0.85rem 1rem;
    margin: 0.5rem 0 0.75rem 0;
  }
  .ak07-saved-box .title {
    color: #4ade80;
    font-weight: 800;
    margin: 0 0 0.25rem 0;
  }
  .ak07-saved-box .mask {
    color: #94a3b8;
    font-family: ui-monospace, monospace;
    font-size: 0.85rem;
    letter-spacing: 0.08em;
  }
  .ak07-connect-row {
    background: #12161d;
    border: 1px solid var(--ak07-border);
    border-radius: 10px;
    padding: 0.85rem 1rem;
    margin: 0;
  }
  .ak07-connect-row .name {
    color: #e6e9ef;
    font-weight: 700;
    margin: 0;
  }
  .ak07-connect-row .desc {
    color: #8b96a8;
    font-size: 0.82rem;
    margin: 0.15rem 0 0 0;
    font-style: italic;
  }
  .ak07-note {
    color: #94a3b8;
    font-size: 0.82rem;
    margin: 0.55rem 0 0.15rem 0;
  }
  .ak07-note strong {
    color: #e2e8f0;
  }
  .ak07-checklist {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    margin: 0 0 0.85rem 0;
  }
  /* Nested Streamlit border containers on broker page */
  div[data-testid="stMain"] [data-testid="stVerticalBlockBorderWrapper"] {
    background: var(--ak07-panel) !important;
    border: 1px solid var(--ak07-border) !important;
    border-radius: 12px !important;
    padding: 0.35rem 0.55rem !important;
    margin-bottom: 0.85rem !important;
  }
  div[data-testid="stMain"] [data-testid="stVerticalBlockBorderWrapper"]
    [data-testid="stVerticalBlockBorderWrapper"] {
    background: #12161d !important;
    border-color: #2a3340 !important;
    margin-top: 0.35rem !important;
    margin-bottom: 0.35rem !important;
  }
  .ak07-check {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border-radius: 999px;
    border: 1px solid var(--ak07-border);
    background: #12161d;
    padding: 0.28rem 0.7rem;
    font-size: 0.8rem;
    font-weight: 600;
    color: #e6e9ef;
  }
  .ak07-check.ok { border-color: #166534; color: #4ade80; }
  .ak07-check.bad { border-color: #7f1d1d; color: #f87171; }
  .ak07-check.warn { border-color: #854d0e; color: #fbbf24; }

  .ak07-reason {
    display: inline-block;
    border-radius: 999px;
    padding: 0.12rem 0.55rem;
    font-size: 0.78rem;
    font-weight: 700;
    border: 1px solid transparent;
  }
  .ak07-reason.tp { background: #14532d; color: #4ade80; border-color: #166534; }
  .ak07-reason.sl { background: #7f1d1d; color: #fca5a5; border-color: #991b1b; }
  .ak07-reason.trail { background: #0c4a6e; color: #7dd3fc; border-color: #0369a1; }
  .ak07-reason.manual { background: #1f2937; color: #e5e7eb; border-color: #4b5563; }

  button[kind="primary"] {
    background-color: #b91c1c !important;
    border: 2px solid #ef4444 !important;
    color: #fff !important;
    font-weight: 800 !important;
    padding: 0.55rem 0.5rem !important;
    width: 100%;
  }

  [data-testid="stExpander"] {
    background-color: var(--ak07-panel) !important;
    border: 1px solid var(--ak07-border) !important;
    border-radius: 8px !important;
  }
  [data-testid="stExpander"] summary,
  [data-testid="stExpander"] summary span,
  [data-testid="stExpander"] summary p { color: #e6e9ef !important; }
  [data-testid="stExpander"] div[data-testid="stExpanderDetails"] {
    background-color: #12161d !important;
    color: #e6e9ef !important;
  }
  .ak07-signal-line {
    color: #e6e9ef;
    font-family: ui-monospace, monospace;
    font-size: 0.88rem;
    margin: 0.15rem 0;
  }

  header[data-testid="stHeader"] { background: transparent; }
  footer { visibility: hidden; }

  /* Charts / dataframes on performance page */
  [data-testid="stVerticalBlock"] > div:has(> div[data-testid="stArrowVegaLiteChart"]),
  [data-testid="stVerticalBlock"] > div:has(> [data-testid="stDataFrame"]) {
    width: 100%;
  }
</style>
"""


def inject_dark_theme() -> None:
    st.markdown(DARK_THEME_CSS, unsafe_allow_html=True)


def inject_broker_connect_styles() -> None:
    """Calmer primary/secondary actions for Broker connect only."""
    st.markdown(
        """
<style>
  div[data-testid="stMain"] button[kind="primary"],
  div[data-testid="stMain"] button[data-testid="baseButton-primary"],
  div[data-testid="stMain"] button[data-testid="stBaseButton-primary"],
  div[data-testid="stMain"] a[data-testid="baseLinkButton-primary"],
  div[data-testid="stMain"] a[data-testid="stBaseLinkButton-primary"] {
    background-color: #2563eb !important;
    border: 1px solid #3b82f6 !important;
    color: #fff !important;
    font-weight: 700 !important;
    box-shadow: 0 6px 16px rgba(37, 99, 235, 0.35) !important;
    width: auto !important;
    min-width: 10rem;
    padding: 0.5rem 1.1rem !important;
    border-radius: 8px !important;
    text-decoration: none !important;
  }
  div[data-testid="stMain"] div[data-testid="stForm"] button[kind="primary"],
  div[data-testid="stMain"] div[data-testid="stForm"] button[data-testid="baseButton-primary"],
  div[data-testid="stMain"] div[data-testid="stForm"] button[data-testid="stBaseButton-primary"],
  div[data-testid="stMain"] div[data-testid="column"] button[kind="primary"],
  div[data-testid="stMain"] div[data-testid="column"] button[data-testid="baseButton-primary"],
  div[data-testid="stMain"] div[data-testid="column"] button[data-testid="stBaseButton-primary"] {
    width: 100% !important;
  }
  div[data-testid="stMain"] div[data-testid="stForm"] button[kind="secondary"],
  div[data-testid="stMain"] div[data-testid="stForm"] button[data-testid="baseButton-secondary"],
  div[data-testid="stMain"] div[data-testid="stForm"] button[data-testid="stBaseButton-secondary"],
  div[data-testid="stMain"] div[data-testid="column"] button[kind="secondary"],
  div[data-testid="stMain"] div[data-testid="column"] button[data-testid="baseButton-secondary"],
  div[data-testid="stMain"] div[data-testid="column"] button[data-testid="stBaseButton-secondary"] {
    background-color: #1f2937 !important;
    border: 1px solid #4b5563 !important;
    color: #e5e7eb !important;
    font-weight: 600 !important;
    box-shadow: none !important;
    width: 100% !important;
    border-radius: 8px !important;
    padding: 0.5rem 1rem !important;
  }
</style>
""",
        unsafe_allow_html=True,
    )


def inject_login_page_style(*, background_path: Path | None = None) -> None:
    """Full-viewport background + centered login card (sign-in page only)."""
    bg_css = ""
    if background_path and background_path.is_file():
        import base64

        mime = "image/png" if background_path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(background_path.read_bytes()).decode("ascii")
        bg_css = f"""
  .stApp {{
    background-color: transparent !important;
    background-image:
      linear-gradient(rgba(12, 15, 20, 0.78), rgba(12, 15, 20, 0.88)),
      url("data:{mime};base64,{encoded}") !important;
    background-size: cover !important;
    background-position: center center !important;
    background-repeat: no-repeat !important;
    background-attachment: fixed !important;
  }}
  [data-testid="stAppViewContainer"] {{
    background: transparent !important;
  }}
"""

    st.markdown(
        f"""
<style>
{bg_css}
  [data-testid="stSidebar"] {{ display: none; }}
  section.main > div.block-container {{
    max-width: 28rem !important;
    margin: 0 auto !important;
    padding: 2rem 1.75rem 1.5rem !important;
    padding-top: 4rem !important;
    background: rgba(22, 27, 36, 0.92);
    border: 1px solid #232b38;
    border-radius: 16px;
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.45);
    backdrop-filter: blur(8px);
  }}
</style>
""",
        unsafe_allow_html=True,
    )


def login_background_path() -> Path:
    from app.config.paths import repo_root

    return repo_root() / "assets" / "branding" / "ak07_login_background.png"


def status_pill(label: str, online: bool | None, detail: str = "") -> str:
    if online is True:
        dot, suffix = "ak07-dot-on", detail
    elif online is False:
        dot, suffix = "ak07-dot-off", detail or "offline"
    else:
        dot, suffix = "ak07-dot-warn", detail or "n/a"
    extra = f" <span style='color:#8b96a8;font-weight:400'>{suffix}</span>" if suffix else ""
    return (
        f'<span class="ak07-pill"><span class="{dot}">●</span> {label}{extra}</span>'
    )


def summary_chip_row(
    *,
    total: int,
    wins: int,
    losses: int,
    pnl_points: float,
) -> str:
    """HTML chip strip: Total / Win / Loss / PnL (dark AK07 styling)."""
    pnl_cls = "win" if pnl_points > 0.01 else "loss" if pnl_points < -0.01 else "muted"
    return (
        '<div class="ak07-chip-row">'
        f'<div class="ak07-chip"><span class="lbl">Total orders</span>'
        f'<span class="val">{total}</span></div>'
        f'<div class="ak07-chip"><span class="lbl">Win</span>'
        f'<span class="val win">{wins}</span></div>'
        f'<div class="ak07-chip"><span class="lbl">Loss</span>'
        f'<span class="val loss">{losses}</span></div>'
        f'<div class="ak07-chip"><span class="lbl">Total P&amp;L (pts)</span>'
        f'<span class="val {pnl_cls}">{pnl_points:+.2f}</span></div>'
        "</div>"
    )


def strategy_card_header(title: str, subtitle: str = "") -> str:
    sub = f'<p class="sub">{subtitle}</p>' if subtitle else ""
    return f'<div class="ak07-strategy-card"><h3>{title}</h3>{sub}</div>'


def broker_card_open(title: str, subtitle: str = "", icon: str = "🛡️") -> str:
    """Section header inside a bordered container."""
    sub = f'<p class="sub">{subtitle}</p>' if subtitle else ""
    return (
        f'<div class="ak07-broker-card"><div class="ak07-sec-title">'
        f'<span class="ico">{icon}</span><div><h3>{title}</h3>{sub}</div>'
        f"</div></div>"
    )


def broker_hero(title: str, subtitle: str) -> str:
    return (
        '<div class="ak07-hero">'
        '<div class="icon">📡</div>'
        "<div>"
        '<p class="eyebrow">Broker login</p>'
        f"<h2>{title}</h2>"
        f"<p>{subtitle}</p>"
        "</div></div>"
    )


def help_banner(text: str, link_label: str = "", link_url: str = "") -> str:
    link = (
        f'<a href="{link_url}" target="_blank" rel="noopener noreferrer">{link_label} ↗</a>'
        if link_label and link_url
        else ""
    )
    return f'<div class="ak07-help-banner"><span>{text}</span>{link}</div>'


def credentials_title(title: str, subtitle: str) -> str:
    return (
        '<div class="ak07-creds-title"><div>'
        f"<h4>{title}</h4>"
        f"<p>{subtitle}</p>"
        "</div></div>"
    )


def credentials_saved_box(masked_key: str) -> str:
    return (
        '<div class="ak07-saved-box">'
        '<p class="title">✓ Credentials saved</p>'
        f'<div class="mask">{masked_key}</div>'
        "</div>"
    )


def connect_row(name: str, desc: str) -> str:
    return (
        '<div class="ak07-connect-row">'
        f'<p class="name">{name}</p>'
        f'<p class="desc">{desc}</p>'
        "</div>"
    )


def status_text(ok: bool, ok_label: str = "Ready", bad_label: str = "Missing") -> str:
    if ok:
        return f'<span class="ak07-status-ok">✓ {ok_label}</span>'
    return f'<span class="ak07-status-bad">· {bad_label}</span>'


def redirect_box(url: str) -> str:
    return f'<div class="ak07-redirect-box">{url}</div>'


def mask_secret(value: str, keep: int = 4) -> str:
    raw = (value or "").strip()
    if not raw:
        return "••••••••"
    if len(raw) <= keep:
        return "•" * 12
    return ("•" * 12) + raw[-keep:]


def checklist_pills(items: list[tuple[str, str]]) -> str:
    """items: list of (label, state) where state is ok|bad|warn."""
    parts: list[str] = ['<div class="ak07-checklist">']
    for label, state in items:
        cls = state if state in ("ok", "bad", "warn") else ""
        mark = "✓" if state == "ok" else "!" if state == "warn" else "·"
        parts.append(f'<span class="ak07-check {cls}">{mark} {label}</span>')
    parts.append("</div>")
    return "".join(parts)


def format_exit_reason_label(reason: str) -> str:
    """Plain-text badge label for dataframes (emoji + short reason)."""
    text = (reason or "").strip()
    if not text or text == "—":
        return "—"
    low = text.lower()
    if "trail" in low:
        return f"◎ {text}"
    if "tp" in low or "target" in low or "booked" in low:
        return f"▲ {text}"
    if "partial" in low or "kill" in low or "manual" in low:
        return f"◆ {text}"
    if "sl" in low or "stop" in low:
        return f"▼ {text}"
    return text
