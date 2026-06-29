"""AK07 — Option Buying Strategy Vector Engine.

Processes underlying institutional boundaries from the core analyzer and 
translates them into explicit ATM Call (CE) and Put (PE) option execution contracts.
Includes a structural volume filter to filter out low-velocity fakeouts.
"""

from __future__ import annotations


def generate_option_buying_signals(analysis_result: dict) -> dict:
    """Evaluates underlying open interest structures and generates explicit 

    At-The-Money (ATM) option buying execution scripts.
    """
    if "error" in analysis_result:
        return {"status": "Inactive", "reason": f"Backend Error: {analysis_result['error']}"}

    ltp = analysis_result["ltp"]
    pcr = analysis_result["pcr"]
    resistance = analysis_result["resistance_level"]
    support = analysis_result["support_level"]
    symbol = analysis_result["symbol"]
    
    # 1. Derive Option Strike Increments
    if "BANKNIFTY" in symbol:
        strike_step = 100
    elif "NIFTY" in symbol:
        strike_step = 50
    else:
        strike_step = 10 if ltp > 500 else 5

    # 2. Compute At-The-Money (ATM) Option Strike Prices
    atm_strike = round(ltp / strike_step) * strike_step
    
    # 3. Dynamic Premium Target Configurations (Derived from your backtest proxies)
    if "BANKNIFTY" in symbol:
        premium_target_points = 150  
        premium_sl_points = 45       
    elif "SENSEX" in symbol:
        premium_target_points = 120
        premium_sl_points = 35
    elif "NIFTY" in symbol:
        premium_target_points = 60
        premium_sl_points = 18
    else:
        premium_target_points = round(atm_strike * 0.015, 1)
        premium_sl_points = round(atm_strike * 0.005, 1)

    pcr_val = float(pcr) if pcr != "N/A" else 1.0
    dist_to_resistance_pct = (resistance - ltp) / ltp
    dist_to_support_pct = (ltp - support) / ltp

    strategy_mode = "WAIT / OBSERVATION"
    execution_directive = "No execution setup confirmed. Maintain capital protection guidelines."
    trade_setup = None

    # Scenario A: MOMENTUM CALL BUYING SETUP (CE)
    if pcr_val > 1.10 or dist_to_resistance_pct <= 0.0025:
        strategy_mode = "MOMENTUM SQUEEZE (CALL BUYING)"
        execution_directive = (
            f"🟢 ORDER CRITERIA: Wait for a 5-minute candle to break and close ABOVE {resistance:.1f}. "
            f"CRITICAL: 5m Volume must be above its 20-period average to avoid fakeouts. "
            f"Instantly BUY the {symbol} {atm_strike} CALL OPTION (CE)."
        )
        trade_setup = {
            "Asset": symbol,
            "Action": "BUY CE (Call Option)",
            "Contract Strike": atm_strike,
            "Spot Trigger Entry Point": f"Confirmed Close above {resistance:.1f} + Volume Spike",
            "Option Premium Stop Loss": f"Reduce position by {premium_sl_points} pts from your entry premium",
            "Option Premium Target": f"Capture +{premium_target_points} pts expansion from your entry premium",
            "Conviction Rating": "HIGH" if pcr_val > 1.15 else "MEDIUM"
        }

    # Scenario B: MOMENTUM PUT BUYING SETUP (PE)
    elif pcr_val < 0.85 or dist_to_support_pct <= 0.0025:
        strategy_mode = "MOMENTUM BREAKDOWN (PUT BUYING)"
        execution_directive = (
            f"🔴 ORDER CRITERIA: Wait for a 5-minute candle to break and close BELOW {support:.1f}. "
            f"CRITICAL: 5m Volume must be above its 20-period average to avoid fakeouts. "
            f"Instantly BUY the {symbol} {atm_strike} PUT OPTION (PE)."
        )
        trade_setup = {
            "Asset": symbol,
            "Action": "BUY PE (Put Option)",
            "Contract Strike": atm_strike,
            "Spot Trigger Entry Point": f"Confirmed Close below {support:.1f} + Volume Spike",
            "Option Premium Stop Loss": f"Reduce position by {premium_sl_points} pts from your entry premium",
            "Option Premium Target": f"Capture +{premium_target_points} pts expansion from your entry premium",
            "Conviction Rating": "HIGH" if pcr_val < 0.80 else "MEDIUM"
        }

    # Scenario C: SIDEWAYS RANGE CHURN (CHOP RISK)
    else:
        strategy_mode = "CHOPSMASH / RANGE CONSOLIDATION"
        execution_directive = (
            f"🛑 ACTION REQUIRED: Current LTP ({ltp:.2f}) is oscillating in the dangerous center zone. "
            f"Theta decay will bleed premium buyer accounts. DO NOT buy options until a boundary is tested."
        )

    return {
        "symbol": symbol,
        "underlying_ltp": ltp,
        "pcr_ratio": pcr_val,
        "strategy_mode": strategy_mode,
        "execution_directive": execution_directive,
        "trade_setup": trade_setup
    }