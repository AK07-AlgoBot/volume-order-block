"""AK07 Deploy — git pull + docker rebuild/restart from the browser.

Uses the Python docker SDK (talks to /var/run/docker.sock directly — no CLI needed).
The cockpit service must have the socket mounted as a volume in docker-compose.yml.
"""

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

# Label filter — only manage containers from this compose project
COMPOSE_PROJECT = "configs"

SERVICES = {
    "All services":          None,
    "engine  (S1)":          "engine",
    "s7_engine  (S7 ORB+)":  "s7_engine",
    "smc_crt_engine  (S2)":  "smc_crt_engine",
    "breakout_engine  (S3)": "breakout_engine",
    "api  (FastAPI)":        "api",
    "cockpit  (Dashboard)":  "cockpit",
    "mcp":                   "mcp",
    "redis":                 "redis",
}


# ---------------------------------------------------------------------------
# Docker SDK helpers
# ---------------------------------------------------------------------------

def _docker_client():
    try:
        import docker  # noqa: PLC0415
        return docker.from_env()
    except Exception as exc:
        return None, str(exc)


def _list_containers() -> list[dict]:
    client = _docker_client()
    if isinstance(client, tuple):
        return []
    try:
        containers = client.containers.list(all=True)
        rows = []
        for c in containers:
            labels = c.labels or {}
            project = labels.get("com.docker.compose.project", "")
            service = labels.get("com.docker.compose.service", "")
            rows.append({
                "name": c.name,
                "project": project,
                "service": service,
                "status": c.status,
                "short_id": c.short_id,
            })
        return rows
    except Exception as exc:
        return [{"name": str(exc), "project": "", "service": "", "status": "error", "short_id": ""}]


def _fmt_containers(rows: list[dict]) -> str:
    if not rows:
        return "(no containers found)"
    lines = [f"{'NAME':<42} {'STATUS':<15} {'PROJECT'}"]
    lines.append("-" * 75)
    for r in rows:
        lines.append(f"{r['name']:<42} {r['status']:<15} {r['project']}")
    return "\n".join(lines)


def _docker_action(action: str, service: str | None, out_box) -> int:
    """restart or rebuild a service via docker compose CLI (subprocess).
    Returns 0 on success.
    """
    if action == "restart":
        cmd = ["docker", "compose", "-f", COMPOSE_FILE, "restart"]
        if service:
            cmd.append(service)
    elif action == "rebuild":
        cmd = ["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--build"]
        if service:
            cmd.append(service)
    elif action == "logs":
        cmd = ["docker", "compose", "-f", COMPOSE_FILE, "logs", "--tail=80"]
        if service:
            cmd.append(service)
    else:
        out_box.error(f"Unknown action: {action}")
        return 1

    return _stream(cmd, out_box)


def _stream(cmd: list[str], out_box) -> int:
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
            out_box.code("\n".join(buf[-60:]), language="bash")
        proc.wait()
        return proc.returncode
    except FileNotFoundError:
        # docker CLI not on PATH — try common locations
        for docker_path in ["/usr/bin/docker", "/usr/local/bin/docker"]:
            if Path(docker_path).exists():
                cmd[0] = docker_path
                return _stream(cmd, out_box)
        buf.append("ERROR: docker CLI not found. Ensure /var/run/docker.sock is mounted and docker is installed.")
        out_box.code("\n".join(buf), language="bash")
        return 1
    except Exception as exc:
        buf.append(f"ERROR: {exc}")
        out_box.code("\n".join(buf), language="bash")
        return 1


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(*args) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=str(REPO), text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "—"


def _git_info() -> dict[str, str]:
    return {
        "branch":  _git("rev-parse", "--abbrev-ref", "HEAD"),
        "commit":  _git("log", "-1", "--format=%h  %s"),
        "author":  _git("log", "-1", "--format=%an  (%ar)"),
        "status":  _git("status", "--short"),
        "remote":  _git("log", "HEAD..@{u}", "--oneline"),
    }


# ---------------------------------------------------------------------------
# Page layout
# ---------------------------------------------------------------------------

st.markdown("# 🚀 Deploy")
st.caption("Pull latest code and rebuild/restart services — no SSH needed.")

st.markdown("---")

# ---- Git status + container list ------------------------------------------
info_col, ps_col = st.columns([1, 2])

with info_col:
    st.markdown("#### Git Status")
    gi = _git_info()
    st.markdown(f"**Branch:** `{gi['branch']}`")
    st.markdown(f"**Last commit:** `{gi['commit']}`")
    st.markdown(f"**Author:** {gi['author']}")
    remote = gi["remote"].strip()
    if remote and remote != "—":
        st.warning(f"Commits available to pull:\n```\n{remote}\n```")
    elif gi["branch"] != "—":
        st.success("Up to date with remote.")
    if gi["status"].strip() and gi["status"] != "—":
        st.caption(f"Uncommitted changes:\n```\n{gi['status']}\n```")

with ps_col:
    st.markdown("#### Running Containers")
    if st.button("🔄 Refresh container list"):
        st.rerun()
    rows = _list_containers()
    st.code(_fmt_containers(rows), language="bash")

st.markdown("---")

# ---- One-click full deploy -------------------------------------------------
st.markdown("#### Full Deploy  *(pull → rebuild → restart all)*")
st.caption("Runs `git pull` then `docker compose up -d --build --remove-orphans`")

if st.button("⚡ Pull & Rebuild All", type="primary"):
    out = st.empty()
    with st.spinner("Running git pull …"):
        rc = _stream(["git", "pull"], out)
    if rc != 0:
        st.error("git pull failed — check output above.")
    else:
        st.success("git pull OK — rebuilding …")
        with st.spinner("Building & restarting (30–60 s) …"):
            rc2 = _stream(
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

col_restart, col_rebuild, col_logs = st.columns(3)

with col_restart:
    if st.button("🔁 Restart", use_container_width=True, help="Fast restart, no rebuild"):
        out = st.empty()
        with st.spinner(f"Restarting {svc_label} …"):
            rc = _docker_action("restart", svc, out)
        if rc == 0:
            st.success(f"✅ {svc_label} restarted.")
        else:
            st.error("Restart failed — see output above.")

with col_rebuild:
    if st.button("🔨 Rebuild & Restart", use_container_width=True, help="Full image rebuild"):
        out = st.empty()
        with st.spinner(f"Rebuilding {svc_label} …"):
            rc = _docker_action("rebuild", svc, out)
        if rc == 0:
            st.success(f"✅ {svc_label} rebuilt.")
        else:
            st.error("Rebuild failed — see output above.")

with col_logs:
    if st.button("📋 Show Logs", use_container_width=True, help="Last 80 lines"):
        out = st.empty()
        _docker_action("logs", svc, out)

st.markdown("---")

# ---- Cleanup old containers ------------------------------------------------
st.markdown("#### Cleanup Old Containers")
st.caption("Stop and remove containers from old/orphaned deployments (e.g. `ak07-*` project).")

all_rows = _list_containers()
old_rows = [r for r in all_rows if r["project"] and r["project"] != COMPOSE_PROJECT]

if old_rows:
    st.warning(f"{len(old_rows)} old container(s) found from other projects:")
    for r in old_rows:
        st.markdown(f"- `{r['name']}` ({r['project']}) — {r['status']}")

    if st.button("🗑️ Stop & Remove Old Containers", type="secondary"):
        client = _docker_client()
        if isinstance(client, tuple):
            st.error(f"Docker SDK unavailable: {client[1]}")
        else:
            errors = []
            for r in old_rows:
                try:
                    c = client.containers.get(r["name"])
                    c.remove(force=True)
                    st.write(f"Removed `{r['name']}`")
                except Exception as exc:
                    errors.append(f"{r['name']}: {exc}")
            if errors:
                st.error("\n".join(errors))
            else:
                st.success("All old containers removed.")
            st.rerun()
else:
    st.success("No orphaned containers from other projects.")

st.markdown("---")
st.caption(
    "Tip: after updating the Upstox token via **3 Token Update**, "
    "use **Restart** on `engine + smc_crt_engine + breakout_engine` to pick it up instantly."
)
