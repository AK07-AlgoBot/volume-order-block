"""Gamma Expiry Observer — live chain feed, paper hero-zero signals only.

NIFTY Tue · BANKNIFTY last Thu · SENSEX Thu weekly.
No broker orders — logs paper signals for cockpit observer + performance store.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config.paths import server_root
from app.services import cache_manager, performance_store, telegram_notifier
from app.services.expiry_calendar import expiry_label, is_index_expiry
from app.services.gamma_expiry_analytics import (
    BLAST_WINDOW_END,
    BLAST_WINDOW_START,
    GammaConfig,
    build_live_snapshot,
)
from app.services.upstox_engine import (
    INDEX_CONFIGS,
    IndexConfig,
    MOCK_MODE,
    PAPER_TRADING,
    UpstoxClient,
    build_upstox_client,
)

logger = logging.getLogger("ak07.gamma_expiry_engine")

IST: Final = ZoneInfo("Asia/Kolkata")
POLL_SECONDS: Final[float] = float(os.environ.get("GAMMA_POLL_SECONDS", "20"))
SESSION_START: Final[dtime] = dtime(9, 15)
SESSION_END: Final[dtime] = dtime(15, 30)
STRATEGY_LABEL: Final[str] = "Gamma Expiry Observer"
STRATEGY_ID: Final[str] = "gamma_expiry"

REFINED_CONFIG_PATH: Final[Path] = server_root() / "data" / "gamma_expiry_config.json"


def _load_gamma_config() -> GammaConfig:
    env_cfg = GammaConfig(
        pin_distance_pct=float(os.environ.get("GAMMA_PIN_DISTANCE_PCT", "0.20")),
        min_idr_pct=float(os.environ.get("GAMMA_MIN_IDR_PCT", "0.55")),
        otm_strikes=int(os.environ.get("GAMMA_OTM_STRIKES", "2")),
        hero_tp_mult=float(os.environ.get("GAMMA_HERO_TP_MULT", "2.0")),
        min_blast_score=int(os.environ.get("GAMMA_MIN_BLAST_SCORE", "55")),
    )
    for path in (
        REFINED_CONFIG_PATH,
        Path(__file__).resolve().parents[5] / "gamma_expiry_config.json",
    ):
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                return GammaConfig(
                    pin_distance_pct=float(raw.get("pin_distance_pct", env_cfg.pin_distance_pct)),
                    min_idr_pct=float(raw.get("min_idr_pct", env_cfg.min_idr_pct)),
                    otm_strikes=int(raw.get("otm_strikes", env_cfg.otm_strikes)),
                    hero_tp_mult=float(raw.get("hero_tp_mult", env_cfg.hero_tp_mult)),
                    min_blast_score=int(raw.get("min_blast_score", env_cfg.min_blast_score)),
                    iv_assumption=float(raw.get("iv_assumption", env_cfg.iv_assumption)),
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("Could not load gamma config %s: %s", path, exc)
    return env_cfg


@dataclass
class IndexGammaState:
    config: IndexConfig
    trade_day: str = ""
    is_expiry: bool = False
    spot: float | None = None
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    prev_spot: float | None = None
    expiry_date: str = ""
    snapshot: dict[str, Any] | None = None
    setup_label: str = "Waiting"
    paper_signals_today: int = 0
    signal_log: list[str] = field(default_factory=list)
    fired_signals: set[str] = field(default_factory=set)


class GammaMarketClient:
    def __init__(self) -> None:
        self._upstox: UpstoxClient | None = None if MOCK_MODE else build_upstox_client()
        self._mock_spots = {"NIFTY": 23950.0, "BANKNIFTY": 58200.0, "SENSEX": 77000.0}

    def refresh_token(self) -> None:
        if self._upstox:
            self._upstox.refresh_access_token_from_disk()

    def get_spot(self, cfg: IndexConfig) -> float | None:
        if self._upstox:
            return self._upstox.get_ltp(cfg.spot_instrument_key)
        self._mock_spots[cfg.code] += (time.time() % 7 - 3) * 2
        return self._mock_spots[cfg.code]

    def get_chain(self, cfg: IndexConfig, on_date: datetime) -> tuple[str | None, list[dict[str, Any]]]:
        if self._upstox:
            expiry = self._upstox.resolve_expiry_on_date(cfg.spot_instrument_key, on_date.date())
            if not expiry:
                return None, []
            return expiry, self._upstox.get_option_chain_for_expiry(cfg.spot_instrument_key, expiry)
        expiry = on_date.date().isoformat()
        spot = self._mock_spots[cfg.code]
        step = cfg.strike_step
        rows = []
        for i in range(-5, 6):
            strike = int(round(spot / step) * step + i * step)
            rows.append(
                {
                    "strike_price": strike,
                    "call_options": {"market_data": {"oi": 1_000_000 - abs(i) * 50_000, "ltp": 50.0}},
                    "put_options": {"market_data": {"oi": 900_000 - abs(i) * 40_000, "ltp": 48.0}},
                }
            )
        return expiry, rows


class GammaExpiryEngine:
    def __init__(self) -> None:
        self.client = GammaMarketClient()
        self.cfg = _load_gamma_config()
        self.states = {code: IndexGammaState(config=cfg) for code, cfg in INDEX_CONFIGS.items()}
        self.backtest_summary = self._load_backtest_summary()
        if self.backtest_summary:
            cache_manager.set_json(
                cache_manager.GAMMA_BACKTEST_KEY,
                self.backtest_summary,
                ttl_seconds=86400,
            )

    def _load_backtest_summary(self) -> dict[str, Any]:
        for path in (
            Path(__file__).resolve().parents[5] / "gamma_expiry_config.json",
            server_root() / "data" / "gamma_expiry_config.json",
        ):
            if path.is_file():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if "refined_config" in raw:
                        return raw
                    return {"refined_config": raw}
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
        cached = cache_manager.get_json(cache_manager.GAMMA_BACKTEST_KEY)
        return cached if isinstance(cached, dict) else {}

    def run(self) -> None:
        logger.info(
            "Gamma Expiry Observer started (PAPER ONLY) — %s",
            "MOCK" if MOCK_MODE else "LIVE FEED",
        )
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("Gamma engine tick failed")
            time.sleep(POLL_SECONDS)

    def tick(self) -> None:
        now = datetime.now(IST)
        self.client.refresh_token()
        self._roll_day(now)

        for state in self.states.values():
            self._process_index(state, now)

        self._publish_global(now)

    def _roll_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        for state in self.states.values():
            if state.trade_day != today:
                state.trade_day = today
                state.is_expiry = is_index_expiry(state.config.code, now.date())
                state.spot = state.day_open = state.day_high = state.day_low = None
                state.prev_spot = None
                state.expiry_date = ""
                state.snapshot = None
                state.paper_signals_today = 0
                state.signal_log = []
                state.fired_signals = set()
                if state.is_expiry:
                    state.setup_label = f"EXPIRY DAY — {expiry_label(state.config.code)}"
                else:
                    state.setup_label = "Non-expiry — observer idle"

    def _process_index(self, state: IndexGammaState, now: datetime) -> None:
        if not state.is_expiry:
            return
        if now.time() < SESSION_START or now.time() > SESSION_END:
            state.setup_label = "Expiry day — session closed"
            return

        spot = self.client.get_spot(state.config)
        if spot is None:
            state.setup_label = "Waiting for spot"
            return

        if state.day_open is None:
            state.day_open = spot
            state.day_high = spot
            state.day_low = spot
        else:
            state.day_high = max(state.day_high or spot, spot)
            state.day_low = min(state.day_low or spot, spot)

        expiry, chain = self.client.get_chain(state.config, now)
        if not expiry or not chain:
            state.setup_label = "Expiry day — chain unavailable"
            return
        state.expiry_date = expiry

        snap = build_live_snapshot(
            cfg=state.config,
            now=now,
            spot=spot,
            day_high=state.day_high or spot,
            day_low=state.day_low or spot,
            day_open=state.day_open or spot,
            expiry_date=expiry,
            expiry_rule=expiry_label(state.config.code),
            chain_rows=chain,
            prev_spot=state.prev_spot,
            cfg_params=self.cfg,
            signal_log=state.signal_log,
        )
        state.snapshot = self._snapshot_dict(snap)
        state.setup_label = snap.observer_detail

        if snap.observer_signal.startswith("HERO_PAPER") and snap.paper_hero:
            fp = f"{snap.observer_signal}:{snap.pin_strike}:{now.strftime('%H:%M')}"
            if fp not in state.fired_signals and now.time() <= BLAST_WINDOW_END:
                state.fired_signals.add(fp)
                state.paper_signals_today += 1
                line = (
                    f"{now.strftime('%H:%M')} {snap.observer_signal} "
                    f"{snap.paper_hero.get('side')} pin={snap.pin_strike} "
                    f"score={snap.blast_score} spot={spot:.0f} — {snap.observer_detail}"
                )
                state.signal_log.append(line)
                if len(state.signal_log) > 30:
                    state.signal_log = state.signal_log[-30:]
                logger.info("[%s] %s", state.config.code, line)
                telegram_notifier.send_message(
                    f"📊 {state.config.display} GAMMA PAPER {snap.observer_signal}\n"
                    f"Pin {snap.pin_strike} · score {snap.blast_score}\n"
                    f"{snap.observer_detail}\n"
                    f"(Observer only — no order placed)"
                )

        state.prev_spot = spot

    def _snapshot_dict(self, snap: Any) -> dict[str, Any]:
        return {
            "index_code": snap.index_code,
            "is_expiry_day": snap.is_expiry_day,
            "expiry_date": snap.expiry_date,
            "expiry_rule": snap.expiry_rule,
            "spot": snap.spot,
            "pin_strike": snap.pin_strike,
            "call_wall": snap.call_wall,
            "put_floor": snap.put_floor,
            "pin_distance_pts": snap.pin_distance_pts,
            "pin_distance_pct": snap.pin_distance_pct,
            "idr_pct": snap.idr_pct,
            "blast_score": snap.blast_score,
            "regime": snap.regime,
            "bias": snap.bias,
            "gamma_flip": snap.gamma_flip,
            "pcr_oi": snap.pcr_oi,
            "atm_iv": snap.atm_iv,
            "blast_window_active": snap.blast_window_active,
            "blast_window": f"{BLAST_WINDOW_START.strftime('%H:%M')}-{BLAST_WINDOW_END.strftime('%H:%M')} IST",
            "observer_signal": snap.observer_signal,
            "observer_detail": snap.observer_detail,
            "paper_hero": snap.paper_hero,
            "config": {
                "pin_distance_pct": self.cfg.pin_distance_pct,
                "min_idr_pct": self.cfg.min_idr_pct,
                "min_blast_score": self.cfg.min_blast_score,
            },
        }

    def _publish_global(self, now: datetime) -> None:
        payload = {
            "timestamp": now.isoformat(),
            "strategy": STRATEGY_LABEL,
            "paper_trading": True,
            "entries_enabled": False,
            "observer_only": True,
            "backtest_refined": self.backtest_summary,
            "indices": {
                code: {
                    "is_expiry": st.is_expiry,
                    "setup_label": st.setup_label,
                    "expiry_date": st.expiry_date,
                    "paper_signals_today": st.paper_signals_today,
                    "signals": st.signal_log[-15:],
                    **(st.snapshot or {}),
                }
                for code, st in self.states.items()
            },
        }
        cache_manager.set_json(cache_manager.GAMMA_STATE_KEY, payload, ttl_seconds=120)
        cache_manager.set_json(
            cache_manager.GAMMA_HEARTBEAT_KEY,
            {
                "at": now.isoformat(),
                "paper_trading": True,
                "observer_only": True,
                "expiry_today": [c for c, s in self.states.items() if s.is_expiry],
            },
            ttl_seconds=120,
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    GammaExpiryEngine().run()


if __name__ == "__main__":
    main()
