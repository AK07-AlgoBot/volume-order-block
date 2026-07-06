"""Dashboard roles and default operator account."""

DASHBOARD_USERNAME = "AK07"
ADMIN_ROLE = "admin"
USER_ROLE = "user"

# Strategy entitlements (dashboard panels + future order fan-out)
STRATEGY_S1_OI = "s1_oi"
STRATEGY_S2_SMC = "s2_smc"
STRATEGY_S3_BREAKOUT = "s3_breakout"
STRATEGY_S7_ORB = "s7_orb"
STRATEGY_S8_CHOCH = "s8_choch"
STRATEGY_GAMMA = "gamma"

# S3 live trading + dashboard panel — Nifty only (BN/Sensex excluded).
S3_BREAKOUT_INDICES: tuple[str, ...] = ("NIFTY",)

ALL_STRATEGIES: tuple[str, ...] = (
    STRATEGY_S1_OI,
    STRATEGY_S2_SMC,
    STRATEGY_S3_BREAKOUT,
    STRATEGY_S7_ORB,
    STRATEGY_S8_CHOCH,
    STRATEGY_GAMMA,
)

STRATEGY_LABELS: dict[str, str] = {
    STRATEGY_S1_OI: "Strategy 1 — AK07 OI",
    STRATEGY_S2_SMC: "Strategy 2 — SMC+CRT",
    STRATEGY_S3_BREAKOUT: "Strategy 3 — BLR Breakout (Nifty)",
    STRATEGY_S7_ORB: "Strategy 7 — ORB+ ADX",
    STRATEGY_S8_CHOCH: "Strategy 8 — CHOCH",
    STRATEGY_GAMMA: "Gamma Expiry Observer",
}

STRATEGY_PILL_SHORT: dict[str, str] = {
    STRATEGY_S1_OI: "S1 OI",
    STRATEGY_S2_SMC: "S2 SMC",
    STRATEGY_S3_BREAKOUT: "S3 BLR",
    STRATEGY_S7_ORB: "S7 ORB+",
    STRATEGY_S8_CHOCH: "S8 CHOCH",
    STRATEGY_GAMMA: "Gamma",
}

SUPPORTED_BROKERS: tuple[str, ...] = ("upstox", "kite", "groww")
