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
STRATEGY_S29_ORB = "s29_orb"
STRATEGY_GC_OF = "gc_of"
STRATEGY_COPY_KITE = "copy_kite"
STRATEGY_GAMMA = "gamma"

# Retired — code retained, not offered live / not shown on dashboard.
RETIRED_STRATEGIES: tuple[str, ...] = (STRATEGY_S7_ORB, STRATEGY_S8_CHOCH)

# S3 live trading + dashboard panel — Nifty only (BN/Sensex excluded).
S3_BREAKOUT_INDICES: tuple[str, ...] = ("NIFTY",)
# S7 ORB+ retired — BankNifty/Sensex engine no longer started.
S7_ORB_INDICES: tuple[str, ...] = ("BANKNIFTY", "SENSEX")
# S29 — Nifty 9:18 1m ORB live (nifty3v4 rules, S3 ITM fan-out).
S29_ORB_INDICES: tuple[str, ...] = ("NIFTY",)
# GoCharting orderflow — Nifty + BankNifty 5m futures alerts → ITM options.
GC_OF_INDICES: tuple[str, ...] = ("NIFTY", "BANKNIFTY")
# Copy Kite — poll leader Kite orders and fan-out via Upstox OMS.

ALL_STRATEGIES: tuple[str, ...] = (
    STRATEGY_S1_OI,
    STRATEGY_S2_SMC,
    STRATEGY_S3_BREAKOUT,
    STRATEGY_S29_ORB,
    STRATEGY_GC_OF,
    STRATEGY_COPY_KITE,
    STRATEGY_GAMMA,
)

STRATEGY_LABELS: dict[str, str] = {
    STRATEGY_S1_OI: "Strategy 1 — AK07 OI",
    STRATEGY_S2_SMC: "Strategy 2 — SMC+CRT",
    STRATEGY_S3_BREAKOUT: "Strategy 3 — BLR Breakout (Nifty)",
    STRATEGY_S7_ORB: "Strategy 7 — ORB+ ADX",
    STRATEGY_S8_CHOCH: "Strategy 8 — CHOCH",
    STRATEGY_S29_ORB: "Strategy 29 — Nifty ORB+",
    STRATEGY_GC_OF: "GoCharting — Orderflow",
    STRATEGY_COPY_KITE: "Copy Kite — Arun mirror",
    STRATEGY_GAMMA: "Gamma Expiry Observer",
}

STRATEGY_PILL_SHORT: dict[str, str] = {
    STRATEGY_S1_OI: "S1 OI",
    STRATEGY_S2_SMC: "S2 SMC",
    STRATEGY_S3_BREAKOUT: "S3 BLR",
    STRATEGY_S7_ORB: "S7 ORB+",
    STRATEGY_S8_CHOCH: "S8 CHOCH",
    STRATEGY_S29_ORB: "S29 ORB",
    STRATEGY_GC_OF: "GC OF",
    STRATEGY_COPY_KITE: "Copy Kite",
    STRATEGY_GAMMA: "Gamma",
}

SUPPORTED_BROKERS: tuple[str, ...] = ("upstox", "kite", "groww")
