from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


Side = Literal["LONG", "SHORT"]


@dataclass
class LotExit:
    """Single lot (contract) partial or full exit."""

    lot_index: int
    exit_ts: datetime
    exit_price: float
    points: float
    reason: str


@dataclass
class SwingTrapTrade:
    """Full trade record for reporting (CSV/JSON)."""

    side: Side
    entry_tf: str
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime | None
    swing_high_ref: float
    swing_low_ref: float
    breakout_level: float
    stop_loss: float
    target_price: float
    risk_per_unit: float
    reward_per_unit: float
    total_points: float
    exit_reason: str
    lot_exits: list[LotExit] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "entry_tf": self.entry_tf,
            "entry_ts": self.entry_ts.isoformat(),
            "entry_price": self.entry_price,
            "exit_ts": self.exit_ts.isoformat() if self.exit_ts else None,
            "swing_high_ref": self.swing_high_ref,
            "swing_low_ref": self.swing_low_ref,
            "breakout_level": self.breakout_level,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "risk_per_unit": self.risk_per_unit,
            "reward_per_unit": self.reward_per_unit,
            "total_points": self.total_points,
            "exit_reason": self.exit_reason,
            "lot_exits": [
                {
                    "lot_index": le.lot_index,
                    "exit_ts": le.exit_ts.isoformat(),
                    "exit_price": le.exit_price,
                    "points": le.points,
                    "reason": le.reason,
                }
                for le in self.lot_exits
            ],
            "meta": self.meta,
        }
