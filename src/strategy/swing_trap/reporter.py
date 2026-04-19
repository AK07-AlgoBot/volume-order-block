from __future__ import annotations

import csv
import json
from pathlib import Path

from strategy.swing_trap.models import SwingTrapTrade


def trades_to_csv(path: Path, trades: list[SwingTrapTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not trades:
        path.write_text("", encoding="utf-8")
        return
    fields = [
        "side",
        "entry_tf",
        "entry_ts",
        "entry_price",
        "exit_ts",
        "swing_high_ref",
        "swing_low_ref",
        "breakout_level",
        "stop_loss",
        "target_price",
        "risk_per_unit",
        "reward_per_unit",
        "total_points",
        "exit_reason",
        "lot_exits_json",
        "meta_json",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            d = t.to_dict()
            row = {
                "side": d["side"],
                "entry_tf": d["entry_tf"],
                "entry_ts": t.entry_ts.isoformat(),
                "entry_price": d["entry_price"],
                "exit_ts": t.exit_ts.isoformat() if t.exit_ts else "",
                "swing_high_ref": d["swing_high_ref"],
                "swing_low_ref": d["swing_low_ref"],
                "breakout_level": d["breakout_level"],
                "stop_loss": d["stop_loss"],
                "target_price": d["target_price"],
                "risk_per_unit": d["risk_per_unit"],
                "reward_per_unit": d["reward_per_unit"],
                "total_points": d["total_points"],
                "exit_reason": d["exit_reason"],
                "lot_exits_json": json.dumps(d.get("lot_exits") or []),
                "meta_json": json.dumps(d.get("meta") or {}),
            }
            w.writerow(row)


def trades_to_json(path: Path, trades: list[SwingTrapTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([t.to_dict() for t in trades], indent=2), encoding="utf-8")
