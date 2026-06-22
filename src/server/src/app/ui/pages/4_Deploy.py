"""AK07 Deploy — git pull + docker rebuild/restart from the browser."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.config.paths import repo_root
from app.ui.styles import inject_dark_theme

st.set_page_config(
    page_title="AK07 — Deploy",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="collapsed",
)

inject_dark_theme()

REPO = repo_root()
COMPOSE_FILE = str(REPO / "configs" / "docker-compose.yml")

SERVICES = {
    "All services":        None,
    "engine  (S1 + S7)":  "engine",
    "smc_crt_engine (S2)": "smc_crt_engine",
    "breakout_engine (S3)": "breakout_engine",
    "api  (FastAPI)":      "api",
    "cockpit  (Dashboard)": "cockpit",
    "mcp":                 "mcp",
    "redis":               "redis",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], output_box) -> int:
    """Run a command, stream output line-by-line into output_box. Returns exit code."""
    buf: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in iter(proc.stdout.readline, ""):
            buf.append(line.rstrip())
            output_box.code("\n".join(buf[-60:]), language="bash")
        proc.wait()
        return proc.returncode
    except Exception as exc:
        buf.append(f"ERROR: {exc}")
        output_box.code("\n".join(buf), language="bash")
        return 1


def _git_info() -> dict[str, str]:
    def _g(*args):
        try:
            return subprocess.check_output(
                ["git", *args], cwd=str(REPO), text=True, stderr=subprocess.DEVNULL
            ).strip()
        except Exception:
            return "—"

    return {
        "branch":  _g("rev-parse", "--abbrev-ref", "HEAD"),
        "commit":  _g("log", "-1", "--format=%h  %s"),
        "author":  _g("log", "-1", "--format=%an  (%ar)"),
        "status":  _g("status", "--short"),
        "remote":  _g("log", "HEAD..@{u}", "--oneline"),
    }


def _docker_ps() -> str:
    try:
        return subprocess.check_output(
            ["docker", "compose", "-f", COMPOSE_FILE, "ps", "--format",
             "table {{.Name}}\t{{.Status}}\t{{.Ports}}"],
            cwd=str(REPO), text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception as exc:
        return f"docker ps failed: {exc}"


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.markdown("# 🚀 Deploy")
st.caption("Pull latest code and rebuild/restart services — no SSH needed.")

st.markdown("---")

# ---- Current state ---------------------------------------------------------
info_col, ps_col = st.columns([1, 2])

with info_col:
    st.markdown("#### Git Status")
    gi = _git_info()
    st.markdown(f"**Branch:** `{gi['branch']}`")
    st.markdown(f"**Last commit:** `{gi['commit']}`")
    st.markdown(f"**Author:** {gi['author']}")
    if gi["remote"].strip():
        st.warning(f"Commits available to pull:\n```\n{gi['remote']}\n```")
    elif gi["remote"] == "—":
        st.caption("(could not check remote)")
    else:
        st.success("Up to date with remote.")
    if gi["status"].strip():
        st.caption(f"Uncommitted changes:\n```\n{gi['status']}\n```")

with ps_col:
    st.markdown("#### Running Containers")
    if st.button("🔄 Refresh container list"):
        st.rerun()
    st.code(_docker_ps(), language="bash")

st.markdown("---")

# ---- One-click full deploy -------------------------------------------------
st.markdown("#### Full Deploy  *(pull → rebuild → restart all)*")
st.caption("This runs: `git pull` then `docker compose up -d --build --remove-orphans`")

if st.button("⚡ Pull & Rebuild All", type="primary", use_container_width=False):
    out = st.empty()
    with st.spinner("Running git pull …"):
        rc = _run(["git", "pull"], out)
    if rc != 0:
        st.error("git pull failed — check output above.")
    else:
        st.success("git pull OK — rebuilding containers …")
        with st.spinner("Building & restarting (this takes ~30–60 s) …"):
            rc2 = _run(
                ["docker", "compose", "-f", COMPOSE_FILE,
                 "up", "-d", "--build", "--remove-orphans"],
                out,
            )
        if rc2 == 0:
            st.success("✅ All containers rebuilt and started.")
            st.rerun()
        else:
            st.error("docker compose up failed — check output above.")

st.markdown("---")

# ---- Per-service controls --------------------------------------------------
st.markdown("#### Per-Service Controls")

svc_label = st.selectbox("Select service", list(SERVICES.keys()), index=0)
svc = SERVICES[svc_label]

col_restart, col_rebuild, col_logs = st.columns([1, 1, 1])

with col_restart:
    if st.button("🔁 Restart (no rebuild)", use_container_width=True):
        cmd = ["docker", "compose", "-f", COMPOSE_FILE, "restart"]
        if svc:
            cmd.append(svc)
        out = st.empty()
        with st.spinner(f"Restarting {svc_label} …"):
            rc = _run(cmd, out)
        if rc == 0:
            st.success(f"✅ {svc_label} restarted.")
        else:
            st.error("Restart failed — check output above.")

with col_rebuild:
    if st.button("🔨 Rebuild & Restart", use_container_width=True):
        cmd = ["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--build"]
        if svc:
            cmd.append(svc)
        out = st.empty()
        with st.spinner(f"Rebuilding {svc_label} …"):
            rc = _run(cmd, out)
        if rc == 0:
            st.success(f"✅ {svc_label} rebuilt and started.")
        else:
            st.error("Rebuild failed — check output above.")

with col_logs:
    if st.button("📋 Show Logs (last 80 lines)", use_container_width=True):
        cmd = ["docker", "compose", "-f", COMPOSE_FILE, "logs", "--tail=80"]
        if svc:
            cmd.append(svc)
        out = st.empty()
        _run(cmd, out)

st.markdown("---")
st.caption(
    "Tip: after updating the Upstox token via **3 Token Update**, "
    "use **Restart (no rebuild)** on `engine` + `smc_crt_engine` + `breakout_engine` "
    "to pick it up instantly."
)
