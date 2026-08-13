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

  html, body, .stApp, [data-testid="stAppViewContainer"],
  [data-testid="stAppViewContainer"] > .main,
  section.main {
    background-color: var(--ak07-bg) !important;
    color: #e6e9ef;
  }
  /* Desktop: collapse Streamlit chrome (mobile keeps header — sidebar toggle lives there). */
  @media (min-width: 769px) {
    .stAppHeader, header.stAppHeader, header[data-testid="stHeader"],
    div[data-testid="stToolbar"],
    div[data-testid="stDecoration"],
    div[data-testid="stStatusWidget"],
    .stAppDeployButton,
    div[data-testid="stAppToolbar"],
    div[data-testid="stToolbarActions"] {
      display: none !important;
      height: 0 !important;
      min-height: 0 !important;
      max-height: 0 !important;
      visibility: hidden !important;
      opacity: 0 !important;
      pointer-events: none !important;
      padding: 0 !important;
      margin: 0 !important;
      border: none !important;
      background: transparent !important;
    }
    :root, .stApp, [data-testid="stAppViewContainer"] {
      --header-height: 0px !important;
    }
    [data-testid="stAppViewContainer"] {
      padding-top: 0 !important;
    }
  }
  [data-testid="stAppViewContainer"] > .main,
  section.main,
  section.stMain,
  [data-testid="stMain"] {
    margin-top: 0 !important;
    padding-top: 0 !important;
  }

  /* --- Full-width main canvas (wide layout without dead side margins) --- */
  [data-testid="stAppViewContainer"],
  [data-testid="stAppViewContainer"] > section.main,
  section.main > div.block-container {
    max-width: none !important;
    width: 100% !important;
  }

  /* Modern Streamlit class + legacy selectors — kill the top gap */
  .stMainBlockContainer,
  .stAppViewBlockContainer,
  section.stMain .block-container,
  section.main > div.block-container,
  [data-testid="stMain"] > div.block-container,
  [data-testid="stMainBlockContainer"],
  div[class*="block-container"] {
    padding-top: 0 !important;
    margin-top: 0 !important;
  }
  /* Keep brand bar flush with sidebar header so the hairlines meet */
  section[data-testid="stMain"] .block-container {
    padding-top: 0 !important;
  }
  section.main > div.block-container,
  [data-testid="stMain"] > div.block-container {
    padding-bottom: 1.5rem !important;
    padding-left: 1.25rem !important;
    padding-right: 1.5rem !important;
  }
  /* Kill leftover top spacer widgets Streamlit inserts above page content */
  section.main > div.block-container > div:first-child,
  [data-testid="stMain"] > div.block-container > div:first-child {
    margin-top: 0 !important;
    padding-top: 0 !important;
  }
  /* Markdown widgets that host the slim topbar should not add vertical slack */
  section.main div[data-testid="stMarkdownContainer"]:has(.ak07-topbar),
  [data-testid="stMain"] div[data-testid="stMarkdownContainer"]:has(.ak07-topbar) {
    margin: 0 !important;
    padding: 0 !important;
  }
  section.main div[data-testid="stMarkdownContainer"]:has(.ak07-topbar) > div,
  [data-testid="stMain"] div[data-testid="stMarkdownContainer"]:has(.ak07-topbar) > div {
    margin: 0 !important;
    padding: 0 !important;
  }
  /* Collapse empty markdown hosts left after browsers hoist <style> out of the body */
  section.main div[data-testid="stMarkdownContainer"]:empty,
  [data-testid="stMain"] div[data-testid="stMarkdownContainer"]:empty,
  section.main .stElementContainer:has(> div[data-testid="stMarkdownContainer"]:empty),
  [data-testid="stMain"] .stElementContainer:has(> div[data-testid="stMarkdownContainer"]:empty) {
    display: none !important;
    height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    min-height: 0 !important;
  }

  /* Shared sidebar chrome (desktop pinning + mobile drawer overrides below) */
  section[data-testid="stSidebar"] {
    background-color: #11151c;
    overflow-x: hidden !important;
    box-sizing: border-box !important;
  }
  section[data-testid="stSidebar"] > div {
    display: flex !important;
    flex-direction: column !important;
    height: 100% !important;
    box-sizing: border-box !important;
    overflow-x: hidden !important;
  }
  section[data-testid="stSidebar"] .block-container {
    width: 100% !important;
    max-width: 100% !important;
    padding-top: 0.5rem !important;
    padding-left: 0.75rem !important;
    padding-right: 0.75rem !important;
    padding-bottom: 0.75rem !important;
    box-sizing: border-box !important;
    overflow-x: hidden !important;
  }
  [data-testid="stSidebarNav"] {
    padding-top: 0.15rem !important;
    width: 100% !important;
    box-sizing: border-box !important;
  }
  /* Profile / sign-out / kill-switch: keep inside sidebar; full-width top rule */
  [data-testid="stSidebarUserContent"] {
    position: relative !important;
    margin-top: auto !important;
    width: 100% !important;
    max-width: 100% !important;
    box-sizing: border-box !important;
    overflow-x: hidden !important;
    border-top: none !important;
    padding-top: 0.85rem !important;
  }
  /* Full-bleed hairline matching the brand bar (not Streamlit's short inset hr) */
  [data-testid="stSidebarUserContent"]::before {
    content: "";
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 1px;
    background: var(--ak07-border);
    pointer-events: none;
  }
  section[data-testid="stSidebar"] hr {
    display: none !important;
  }
  section[data-testid="stSidebar"] button,
  section[data-testid="stSidebar"] [data-testid="stBaseButton-primary"],
  section[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"],
  section[data-testid="stSidebar"] [data-testid="baseButton-primary"],
  section[data-testid="stSidebar"] [data-testid="baseButton-secondary"] {
    max-width: 100% !important;
    width: 100% !important;
    box-sizing: border-box !important;
  }
  section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"],
  section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
    max-width: 100% !important;
    overflow-wrap: anywhere !important;
  }

  /* Desktop: sidebar always open in flex flow (no margin-left on main). */
  @media (min-width: 769px) {
    section[data-testid="stSidebar"] {
      width: 16rem !important;
      min-width: 16rem !important;
      max-width: 16rem !important;
      transform: none !important;
      visibility: visible !important;
      flex: 0 0 16rem !important;
    }
    section[data-testid="stSidebar"] > div {
      width: 16rem !important;
      min-width: 16rem !important;
      max-width: 16rem !important;
    }
    [data-testid="stSidebarCollapseButton"],
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="collapsedControl"],
    [data-testid="stExpandSidebarButton"],
    button[kind="headerNoPadding"] {
      display: none !important;
    }
  }

  /* Content pane uses remaining width beside sidebar — no extra offset */
  [data-testid="stAppViewContainer"] [data-testid="stMain"],
  [data-testid="stAppViewContainer"] section.stMain,
  section.stMain {
    margin-left: 0 !important;
    flex: 1 1 auto !important;
    min-width: 0 !important;
    overflow-x: clip !important; /* stop topbar bleed from creating a page scrollbar */
  }
  [data-testid="stAppViewContainer"] [data-testid="stMain"] > div.block-container,
  [data-testid="stAppViewContainer"] section.stMain > div.block-container,
  section.stMain > div.block-container,
  .stMainBlockContainer {
    width: 100% !important;
    max-width: none !important;
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
    width: 100%;
    box-sizing: border-box;
    background: #12161d;
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

  .ak07-strategy-card,
  .ak07-sec-head {
    background: transparent;
    border: none;
    border-radius: 0;
    padding: 0;
    margin: 0 0 0.45rem 0;
  }
  .ak07-strategy-card h3,
  .ak07-sec-head h3 {
    margin: 0;
    font-size: 1.12rem;
    color: #f1f5f9;
    font-weight: 800;
  }
  .ak07-strategy-card .sub,
  .ak07-sec-head .sub {
    margin: 0.2rem 0 0.35rem 0;
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

  .stAppDeployButton { display: none !important; }
  footer { visibility: hidden; }

  .ak07-funds-card {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: stretch;
    gap: 0;
    width: fit-content;
    max-width: 100%;
    margin: 0 0 0.45rem 0;
    background: var(--ak07-panel);
    border: 1px solid var(--ak07-border);
    border-radius: 12px;
    overflow: hidden;
  }
  .ak07-funds-cell {
    flex: 0 1 auto;
    min-width: 11rem;
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.85rem 1.15rem;
  }
  .ak07-funds-cell + .ak07-funds-cell {
    border-left: 1px solid var(--ak07-border);
  }
  .ak07-funds-ico {
    width: 2.35rem;
    height: 2.35rem;
    border-radius: 8px;
    background: #2563eb;
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
    font-size: 1.05rem;
  }
  .ak07-funds-cell .lbl {
    margin: 0;
    color: #94a3b8;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .ak07-funds-cell .val {
    margin: 0.15rem 0 0 0;
    color: #f1f5f9;
    font-size: 1.35rem;
    font-weight: 800;
    line-height: 1.15;
  }
  .ak07-funds-cell .val.win { color: #4ade80; }
  .ak07-funds-cell .val.loss { color: #f87171; }
  .ak07-funds-cell .val.muted { color: #94a3b8; font-size: 1.15rem; }

  /* Utility strip — same height + hairline as sidebar brand bar */
  .ak07-topbar {
    --ak07-brand-bar-h: 4.5rem;
    position: relative;
    top: 0;
    z-index: 1000;
    display: flex;
    flex-wrap: nowrap;
    align-items: center;
    justify-content: flex-end;
    gap: 0.35rem;
    /* Bleed left to meet sidebar hairline; keep right inset so avatar stays in view */
    margin: 0 0 0.45rem -1.25rem;
    padding: 0 0.35rem 0 1.25rem;
    width: calc(100% + 1.25rem);
    max-width: calc(100% + 1.25rem);
    min-height: var(--ak07-brand-bar-h) !important;
    height: var(--ak07-brand-bar-h) !important;
    box-sizing: border-box;
    background: var(--ak07-bg);
    border: none;
    border-radius: 0;
    box-shadow: none;
    border-bottom: 1px solid var(--ak07-border);
    overflow: hidden;
  }
  .ak07-topbar-brand {
    display: none; /* mobile header only */
    font-weight: 800;
    font-size: 1.05rem;
    letter-spacing: -0.02em;
    color: #f8fafc;
    line-height: 1;
  }
  /* Mobile page navbar — hidden on desktop (sidebar owns navigation) */
  .ak07-mobile-nav {
    display: none;
  }
  .ak07-topbar-meta {
    display: flex;
    flex-wrap: nowrap;
    align-items: center;
    justify-content: flex-end;
    gap: 0.3rem 0.35rem;
    margin-left: auto;
    margin-right: 0;
    flex: 0 1 auto;
    min-width: 0;
    max-width: 100%;
  }
  .ak07-topbar-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    border: 1px solid var(--ak07-border);
    background: #161b24;
    color: #cbd5e1;
    border-radius: 999px;
    padding: 0.22rem 0.6rem;
    font-size: 0.74rem;
    font-weight: 600;
    white-space: nowrap;
    line-height: 1.2;
  }
  .ak07-topbar-chip.accent {
    background: rgba(37, 99, 235, 0.18);
    border-color: #2563eb;
    color: #bfdbfe;
  }
  .ak07-topbar-chip.accent a {
    color: #bfdbfe !important;
  }
  .ak07-topbar-chip .dot {
    width: 0.45rem;
    height: 0.45rem;
    border-radius: 999px;
    background: #22c55e;
    box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.15);
  }
  .ak07-topbar-chip a {
    color: inherit !important;
    text-decoration: none !important;
    font-weight: 700;
  }
  .ak07-topbar-avatar {
    flex: 0 0 1.85rem;
    width: 1.85rem;
    height: 1.85rem;
    margin-right: 0.15rem;
    border-radius: 999px;
    background: #4c1d95;
    color: #f5f3ff;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
    font-size: 0.8rem;
    border: 1px solid #6d28d9;
  }

  /* Brand at sidebar top — same bar height + same hairline as .ak07-topbar */
  section[data-testid="stSidebar"] [data-testid="stSidebarHeader"],
  [data-testid="stSidebarHeader"] {
    --ak07-brand-bar-h: 4.5rem;
    position: relative !important;
    height: var(--ak07-brand-bar-h) !important;
    min-height: var(--ak07-brand-bar-h) !important;
    width: 100% !important;
    max-width: none !important;
    align-self: stretch !important;
    display: flex !important;
    align-items: center !important;
    gap: 0.55rem !important;
    padding: 0 0.75rem !important;
    margin: 0 !important;
    border: none !important;
    border-bottom: 1px solid var(--ak07-border) !important;
    background: #11151c !important;
    overflow: hidden !important; /* keep mark + text inside the bar above the line */
    box-sizing: border-box !important;
  }
  /* Hide Streamlit's main-area logo copy if present — brand lives in the sidebar only */
  [data-testid="stHeader"] [data-testid="stLogo"],
  [data-testid="stHeader"] [data-testid="stLogoLink"],
  header[data-testid="stHeader"] img,
  .stAppHeader [data-testid="stLogo"] {
    display: none !important;
  }
  [data-testid="stSidebarHeader"] [data-testid="stLogo"],
  [data-testid="stSidebarHeader"] [data-testid="stLogoLink"] {
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    margin: 0 !important;
    padding: 0 !important;
    flex: 0 0 2.75rem !important;
    width: 2.75rem !important;
    height: 2.75rem !important;
    overflow: hidden !important;
    border-radius: 8px !important;
    border: 1px solid var(--ak07-border) !important;
    background: #0c0f14 !important;
  }
  [data-testid="stSidebarHeader"] img,
  [data-testid="stLogo"] img {
    display: block !important;
    width: 100% !important;
    height: 100% !important;
    max-width: 100% !important;
    max-height: 100% !important;
    border-radius: 0 !important;
    border: none !important;
    object-fit: contain !important;
    object-position: center !important;
    background: #0c0f14 !important;
  }
  /* Two clean lines only — do not re-wrap "Algo Trading" */
  [data-testid="stSidebarHeader"]::after {
    content: "AK07\\00000aAlgo Trading";
    white-space: pre; /* pre (not pre-line) prevents "Algo" / "Tradi" wrap */
    display: block;
    color: #ffffff;
    font-size: 1.15rem;
    font-weight: 800;
    line-height: 1.2;
    letter-spacing: -0.02em;
    flex: 1 1 auto;
    min-width: 6.75rem;
    overflow: visible;
  }

  /* Dashboard + each strategy: same card shell (reference-style sections) */
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.ak07-dash-title),
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.ak07-sec-head),
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.ak07-strategy-card),
  div[class*="st-key-ak07_dash_home"],
  div[class*="st-key-ak07_s1"],
  div[class*="st-key-ak07_s2"],
  div[class*="st-key-ak07_s3"],
  div[class*="st-key-ak07_s7"],
  div[class*="st-key-ak07_gamma"] {
    background: var(--ak07-panel) !important;
    border: 1px solid var(--ak07-border) !important;
    border-radius: 12px !important;
    padding: 0.75rem 0.95rem 0.85rem 0.95rem !important;
    margin: 0 0 0.55rem 0 !important;
  }
  .ak07-dash-title {
    margin: 0 0 0.2rem 0 !important;
    color: #f8fafc !important;
    font-size: 1.35rem !important;
    font-weight: 800 !important;
    line-height: 1.2 !important;
  }
  .ak07-dash-sub {
    margin: 0 0 0.55rem 0 !important;
    color: #94a3b8 !important;
    font-size: 0.82rem !important;
    line-height: 1.35 !important;
    border: none !important;
    padding: 0 !important;
  }
  /* No extra divider line inside Dashboard card above the pills */
  div[class*="st-key-ak07_dash_home"] hr {
    display: none !important;
  }
  /* Kill Streamlit's default spacer under bordered blocks */
  div[class*="st-key-ak07_dash_home"] + div,
  div[class*="st-key-ak07_s3"] + div {
    margin-top: 0 !important;
  }
  section.main hr,
  [data-testid="stMain"] hr {
    display: none !important;
    margin: 0 !important;
  }

  /* Charts / dataframes on performance page */
  [data-testid="stVerticalBlock"] > div:has(> div[data-testid="stArrowVegaLiteChart"]),
  [data-testid="stVerticalBlock"] > div:has(> [data-testid="stDataFrame"]) {
    width: 100%;
  }

  /* ========== Mobile / narrow viewports ========== */
  @media (max-width: 768px) {
    /* Full-width main; drawer nav instead of pinned sidebar */
    section.main > div.block-container,
    [data-testid="stMain"] > div.block-container,
    .stMainBlockContainer {
      padding-left: 0.75rem !important;
      padding-right: 0.75rem !important;
      padding-bottom: 1.25rem !important;
    }

    /* Drawer sidebar (overlay) — does not crush main content width */
    section[data-testid="stSidebar"] {
      position: fixed !important;
      left: 0 !important;
      top: 0 !important;
      bottom: 0 !important;
      height: 100% !important;
      width: min(18rem, 88vw) !important;
      min-width: 0 !important;
      max-width: min(18rem, 88vw) !important;
      flex: 0 0 auto !important;
      z-index: 1000400 !important;
      box-shadow: 8px 0 28px rgba(0, 0, 0, 0.45);
    }
    section[data-testid="stSidebar"] > div {
      width: 100% !important;
      min-width: 0 !important;
      max-width: 100% !important;
    }
    [data-testid="stAppViewContainer"] [data-testid="stMain"],
    [data-testid="stAppViewContainer"] section.stMain,
    section.stMain {
      width: 100% !important;
      max-width: 100% !important;
      margin-left: 0 !important;
    }

    /* Mobile navbar: Streamlit header hosts the sidebar (pages) toggle */
    :root, .stApp, [data-testid="stAppViewContainer"] {
      --header-height: 3.5rem !important;
    }
    .stAppHeader,
    header.stAppHeader,
    header[data-testid="stHeader"] {
      display: flex !important;
      align-items: center !important;
      height: 3.5rem !important;
      min-height: 3.5rem !important;
      max-height: 3.5rem !important;
      visibility: visible !important;
      opacity: 1 !important;
      pointer-events: auto !important;
      padding: 0 0.65rem !important;
      margin: 0 !important;
      border: none !important;
      border-bottom: 1px solid var(--ak07-border) !important;
      background: #11151c !important;
      overflow: visible !important;
      z-index: 1000300 !important;
    }
    /* Expand button lives inside the toolbar — must unhide the whole chain */
    header[data-testid="stHeader"] div[data-testid="stToolbar"],
    header[data-testid="stHeader"] [data-testid="stToolbar"],
    div[data-testid="stToolbar"] {
      display: flex !important;
      align-items: center !important;
      visibility: visible !important;
      opacity: 1 !important;
      height: 3.5rem !important;
      min-height: 3.5rem !important;
      max-height: none !important;
      pointer-events: auto !important;
      padding: 0 !important;
      margin: 0 !important;
      background: transparent !important;
      border: none !important;
    }
    header[data-testid="stHeader"] div[data-testid="stToolbar"] *,
    div[data-testid="stToolbar"] > div {
      visibility: visible !important;
      opacity: 1 !important;
      pointer-events: auto !important;
    }
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="collapsedControl"],
    [data-testid="stExpandSidebarButton"],
    [data-testid="stSidebarCollapseButton"],
    [data-testid="stBaseButton-headerNoPadding"],
    button[kind="headerNoPadding"][data-testid="stExpandSidebarButton"],
    button[kind="headerNoPadding"][data-testid="stBaseButton-headerNoPadding"] {
      display: inline-flex !important;
      visibility: visible !important;
      opacity: 1 !important;
      pointer-events: auto !important;
      position: relative !important;
      top: auto !important;
      left: auto !important;
      width: 2.6rem !important;
      height: 2.6rem !important;
      min-width: 2.6rem !important;
      min-height: 2.6rem !important;
      border-radius: 10px !important;
      background: #161b24 !important;
      border: 1px solid var(--ak07-border) !important;
      box-shadow: none !important;
      color: #f8fafc !important;
    }
    [data-testid="stExpandSidebarButton"] [data-testid="stIconMaterial"],
    [data-testid="stExpandSidebarButton"] span {
      color: #f8fafc !important;
      font-size: 1.35rem !important;
    }
    [data-testid="stSidebarCollapseButton"] {
      display: flex !important;
      visibility: visible !important;
      opacity: 1 !important;
      pointer-events: auto !important;
    }
    /* Hide Streamlit clutter; keep sidebar toggle */
    div[data-testid="stDecoration"],
    div[data-testid="stStatusWidget"],
    .stAppDeployButton,
    div[data-testid="stAppToolbar"],
    div[data-testid="stToolbarActions"],
    [data-testid="stMainMenuButton"] {
      display: none !important;
    }

    .ak07-topbar {
      flex-wrap: nowrap;
      justify-content: space-between;
      align-items: center;
      height: auto !important;
      min-height: 2.85rem !important;
      margin: 0 -0.75rem 0.35rem -0.75rem;
      padding: 0.45rem 0.75rem;
      width: calc(100% + 1.5rem);
      max-width: calc(100% + 1.5rem);
      gap: 0.35rem;
    }
    .ak07-topbar-brand {
      display: block;
      flex: 0 0 auto;
    }
    .ak07-topbar-meta {
      justify-content: flex-end;
      width: 100%;
      flex-wrap: wrap;
    }
    .ak07-topbar-chip {
      font-size: 0.7rem;
      padding: 0.2rem 0.5rem;
    }

    /* Always-visible page navbar on phones (sidebar may be collapsed) */
    .ak07-mobile-nav {
      display: flex !important;
      flex-wrap: nowrap;
      align-items: center;
      gap: 0.4rem;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: none;
      margin: 0 -0.75rem 0.65rem -0.75rem;
      padding: 0 0.75rem 0.15rem 0.75rem;
      width: calc(100% + 1.5rem);
      max-width: calc(100% + 1.5rem);
      box-sizing: border-box;
    }
    .ak07-mobile-nav::-webkit-scrollbar {
      display: none;
    }
    .ak07-mobile-nav-link {
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      padding: 0.38rem 0.7rem;
      border-radius: 999px;
      border: 1px solid var(--ak07-border);
      background: #161b24;
      color: #e2e8f0 !important;
      text-decoration: none !important;
      font-size: 0.78rem;
      font-weight: 700;
      white-space: nowrap;
      line-height: 1.2;
    }
    .ak07-mobile-nav-link:hover {
      border-color: #3b82f6;
      color: #bfdbfe !important;
    }

    .ak07-funds-card {
      display: flex;
      flex-direction: column;
      width: 100%;
    }
    .ak07-funds-cell {
      min-width: 0;
      width: 100%;
      padding: 0.75rem 0.9rem;
    }
    .ak07-funds-cell + .ak07-funds-cell {
      border-left: none;
      border-top: 1px solid var(--ak07-border);
    }
    .ak07-funds-cell .val {
      font-size: 1.2rem;
    }

    .ak07-status-bar {
      padding: 0.45rem 0.65rem;
      font-size: 0.78rem;
    }
    .ak07-pill {
      white-space: normal;
      line-height: 1.25;
    }

    /* Stack metric / filter rows into 2-col (or full-width) grids */
    div[data-testid="stHorizontalBlock"] {
      flex-wrap: wrap !important;
      gap: 0.4rem 0.5rem !important;
    }
    div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"],
    div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
      min-width: min(100%, 9.75rem) !important;
      flex: 1 1 calc(50% - 0.5rem) !important;
      width: auto !important;
    }
    /* Status + refresh row: always stack */
    div[data-testid="stHorizontalBlock"]:has(.ak07-status-bar) > div[data-testid="stColumn"],
    div[data-testid="stHorizontalBlock"]:has(.ak07-status-bar) > div[data-testid="column"] {
      flex: 1 1 100% !important;
      min-width: 100% !important;
    }

    .ak07-chip-row {
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
    }
    .ak07-chip {
      flex: 1 1 calc(50% - 0.4rem);
      min-width: 7.5rem;
    }

    div[data-testid="stMetric"] {
      padding: 6px 8px;
    }
    div[data-testid="stMetricValue"] {
      font-size: 1rem !important;
    }

    .ak07-dash-title {
      font-size: 1.15rem !important;
    }
    .ak07-signal-line {
      font-size: 0.8rem;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    div[data-testid="stVerticalBlockBorderWrapper"]:has(.ak07-dash-title),
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.ak07-sec-head),
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.ak07-strategy-card),
    div[class*="st-key-ak07_dash_home"],
    div[class*="st-key-ak07_s1"],
    div[class*="st-key-ak07_s2"],
    div[class*="st-key-ak07_s3"],
    div[class*="st-key-ak07_s7"],
    div[class*="st-key-ak07_gamma"] {
      padding: 0.65rem 0.7rem 0.75rem 0.7rem !important;
    }

    /* Tables / charts: scroll instead of overflowing the viewport */
    div[data-testid="stDataFrame"],
    div[data-testid="stTable"],
    div[data-testid="stArrowVegaLiteChart"] {
      max-width: 100% !important;
      overflow-x: auto !important;
    }

    .stTabs [data-baseweb="tab"] {
      padding: 0.4rem 0.65rem;
      font-size: 0.82rem;
    }

    /* Larger tap targets */
    div[data-testid="stMain"] button,
    div[data-testid="stMain"] [data-testid="stBaseButton-primary"],
    div[data-testid="stMain"] [data-testid="stBaseButton-secondary"] {
      min-height: 2.6rem !important;
    }
  }

  @media (max-width: 420px) {
    div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"],
    div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
      flex: 1 1 100% !important;
      min-width: 100% !important;
    }
    .ak07-chip {
      flex: 1 1 100%;
    }
    .ak07-topbar-chip:not(.accent) {
      display: none;
    }
  }
</style>
"""


def inject_dark_theme() -> None:
    """Inject cockpit CSS once per script run.

    Use st.markdown (not st.html) so rules apply to the app document. st.html can
    isolate styles and leave the unstyled logo at full PNG size, blowing up the layout.
    """
    if st.session_state.get("_ak07_dark_theme_injected"):
        return
    st.session_state["_ak07_dark_theme_injected"] = True
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
  @media (max-width: 480px) {{
    section.main > div.block-container {{
      max-width: calc(100% - 1.25rem) !important;
      margin: 0.75rem auto !important;
      padding: 1.35rem 1rem 1.15rem !important;
      border-radius: 14px;
    }}
    .stApp {{
      background-attachment: scroll !important;
    }}
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
