"""
Stop and start trading_bot.py using trading_bot.lock (under src/bot/).

Used by the dashboard API after Upstox credentials are saved so EC2 users do not need
SSH to recycle the bot. Disable with DASHBOARD_RESTART_BOT_ON_SAVE=0.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# repo/src/bot/this_file.py
REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_FILE = REPO_ROOT / "src" / "bot" / "trading_bot.lock"
BOT_SCRIPT = REPO_ROOT / "src" / "bot" / "trading_bot.py"


def _lock_pid() -> int | None:
    if not LOCK_FILE.exists():
        return None
    try:
        data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        pid = int(data.get("pid", -1))
        return pid if pid > 0 else None
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _looks_like_trading_bot(pid: int) -> bool:
    if os.name == "nt":
        return True
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        joined = raw.replace(b"\x00", b" ")
        return b"trading_bot.py" in joined
    except OSError:
        return False


def terminate_trading_bot(timeout_sec: float = 20.0) -> dict:
    pid = _lock_pid()
    if pid is None:
        return {"stopped": False, "pid": None, "note": "no lock file"}
    if not _pid_alive(pid):
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return {"stopped": False, "pid": pid, "note": "pid not running"}
    if not _looks_like_trading_bot(pid):
        return {"stopped": False, "pid": pid, "note": "lock pid does not look like trading_bot.py"}

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.25)

    if _pid_alive(pid):
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid), "/T"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.5)

    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    return {"stopped": True, "pid": pid}


def start_trading_bot_detached() -> dict:
    if not BOT_SCRIPT.is_file():
        return {"started": False, "error": f"Missing {BOT_SCRIPT}"}
    exe = os.environ.get("TRADING_BOT_PYTHON", "").strip() or sys.executable
    kwargs: dict = {
        "cwd": str(REPO_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if hasattr(subprocess, "DETACHED_PROCESS"):
            flags |= subprocess.DETACHED_PROCESS
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags |= subprocess.CREATE_NO_WINDOW
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen([exe, str(BOT_SCRIPT)], **kwargs)
    except OSError as e:
        return {"started": False, "error": str(e)}
    return {"started": True, "child_pid": proc.pid}


def restart_trading_bot_after_credential_save() -> dict:
    flag = os.environ.get("DASHBOARD_RESTART_BOT_ON_SAVE", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return {"restarted": False, "skipped": "DASHBOARD_RESTART_BOT_ON_SAVE disabled"}

    unit = os.environ.get("DASHBOARD_SYSTEMD_UNIT", "").strip()
    if unit and os.name != "nt":
        try:
            proc = subprocess.run(
                ["systemctl", "restart", unit],
                capture_output=True,
                text=True,
                timeout=90,
            )
            ok = proc.returncode == 0
            err = ((proc.stderr or "") + (proc.stdout or "")).strip()[:500]
            return {
                "restarted": ok,
                "mode": "systemd",
                "unit": unit,
                "systemctl_message": err or None,
            }
        except Exception as e:
            return {"restarted": False, "mode": "systemd", "unit": unit, "error": str(e)}

    stop = terminate_trading_bot()
    time.sleep(0.75)
    start = start_trading_bot_detached()
    return {
        "restarted": bool(start.get("started")),
        "mode": "process",
        "terminate": stop,
        "spawn": start,
    }
