"""Shared Streamlit dark theme + compact layout for AK07 cockpit pages."""

from __future__ import annotations

import streamlit as st

DARK_THEME_CSS = """
<style>
  .stApp { background-color: #0c0f14; color: #e6e9ef; }
  section[data-testid="stSidebar"] {
    background-color: #11151c;
    width: 17rem !important;
    min-width: 17rem !important;
  }
  section[data-testid="stSidebar"] > div {
    width: 17rem !important;
    min-width: 17rem !important;
  }
  div[data-testid="stMetric"] {
    background-color: #161b24; border: 1px solid #232b38;
    border-radius: 10px; padding: 10px 12px;
  }
  div[data-testid="stMetric"] label { color: #8b96a8 !important; font-size: 0.78rem !important; }
  div[data-testid="stMetricValue"] { color: #e6e9ef !important; font-size: 1.15rem !important; }
  .stApp, .stApp p, .stApp li, .stApp label, .stApp span { color: #e6e9ef; }
  [data-testid="stCaptionContainer"], .stCaption { color: #b8c4d4 !important; }
  .ak07-muted-line { color: #b8c4d4; font-size: 0.85rem; }
  div[data-testid="stDataFrame"] { border: 1px solid #232b38; border-radius: 8px; }
  .ak07-status-bar {
    display: flex; flex-wrap: wrap; gap: 0.55rem 1rem; align-items: center;
    background: #161b24; border: 1px solid #232b38; border-radius: 10px;
    padding: 0.55rem 0.85rem; margin-bottom: 0.75rem; font-size: 0.82rem;
  }
  .ak07-pill {
    display: inline-flex; align-items: center; gap: 0.35rem;
    padding: 0.2rem 0.55rem; border-radius: 999px; font-weight: 600;
    border: 1px solid #2a3344; background: #12161d;
  }
  .ak07-dot-on { color: #4ade80; }
  .ak07-dot-off { color: #f87171; }
  .ak07-dot-warn { color: #fbbf24; }
  .ak07-block {
    border-radius: 8px; padding: 10px 6px; text-align: center;
    font-weight: 600; font-size: 0.85rem; color: #ffffff;
    margin-bottom: 6px;
  }
  .ak07-green { background-color: #14532d; border: 1px solid #22c55e; }
  .ak07-red { background-color: #7f1d1d; border: 1px solid #ef4444; }
  .ak07-gray { background-color: #1f2937; border: 1px solid #4b5563; }
  .ak07-badge {
    display: inline-block; border-radius: 6px; padding: 4px 12px;
    font-weight: 700; letter-spacing: 0.05em;
  }
  .ak07-bull { background: #14532d; color: #4ade80; }
  .ak07-bear { background: #7f1d1d; color: #f87171; }
  .ak07-neutral { background: #1f2937; color: #9ca3af; }
  button[kind="primary"] {
    background-color: #b91c1c !important; border: 2px solid #ef4444 !important;
    color: #fff !important; font-weight: 800 !important;
    padding: 0.55rem 0.5rem !important; width: 100%;
  }
  [data-testid="stExpander"] {
    background-color: #161b24 !important;
    border: 1px solid #232b38 !important;
    border-radius: 8px !important;
  }
  [data-testid="stExpander"] summary,
  [data-testid="stExpander"] summary span,
  [data-testid="stExpander"] summary p { color: #e6e9ef !important; }
  [data-testid="stExpander"] div[data-testid="stExpanderDetails"] {
    background-color: #12161d !important; color: #e6e9ef !important;
  }
  .ak07-signal-line {
    color: #e6e9ef; font-family: ui-monospace, monospace;
    font-size: 0.88rem; margin: 0.15rem 0;
  }
  header[data-testid="stHeader"] { background: transparent; }
  footer { visibility: hidden; }
</style>
"""


def inject_dark_theme() -> None:
    st.markdown(DARK_THEME_CSS, unsafe_allow_html=True)


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
