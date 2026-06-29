"""Stock OI analyzer — Dynamic Institutional Structure Engine.

Extracts maximum Open Interest strikes directly from the Upstox API payload.
Includes an intelligent volumetric operational directive inside the narrative.
"""

from __future__ import annotations

from datetime import date
import requests
import pandas as pd

BASE_URL = "https://api.upstox.com/v2"

SYMBOL_MAP = {
    "CRUDEOIL": "MCX_FO|CRUDEOIL",
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "RELIANCE": "NSE_EQ|INE002A01018",
}


def load_credentials_for_user(username: str = "AK07") -> tuple[str, str]:
    """Load Upstox access token and base URL from per-user credentials file."""
    from upstox_credentials_store import load_upstox_credentials_for_user # noqa: PLC0415
    
    creds = load_upstox_credentials_for_user(username)
    token = creds.get("access_token") or ""
    base_url = creds.get("base_url") or BASE_URL
    return token, base_url.rstrip("/")


def get_headers(access_token: str) -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": f"Bearer {access_token}"}


def _quote_from_data(data: dict, instrument_key: str) -> dict:
    if not isinstance(data, dict): return {}
    quote = data.get(instrument_key)
    if isinstance(quote, dict): return quote
    alt_key = instrument_key.replace("|", ":", 1)
    quote = data.get(alt_key)
    if isinstance(quote, dict): return quote
    for row in data.values():
        if isinstance(row, dict) and row.get("last_price") is not None: return row
    return {}


def fetch_market_quote(instrument_key: str, access_token: str, base_url: str = BASE_URL):
    url = f"{base_url}/market-quote/ltp"
    params = {"instrument_key": instrument_key}
    try:
        response = requests.get(url, params=params, headers=get_headers(access_token), timeout=20)
        if response.status_code == 200:
            payload = response.json()
            if payload.get("status") != "success": return None
            quote = _quote_from_data(payload.get("data", {}), instrument_key)
            ltp = quote.get("last_price")
            return float(ltp) if ltp is not None else None
        return None
    except Exception: return None


def fetch_option_chain(instrument_key: str, expiry_date: str, access_token: str, base_url: str = BASE_URL):
    url = f"{base_url}/option/chain"
    params = {"instrument_key": instrument_key, "expiry_date": expiry_date}
    try:
        response = requests.get(url, params=params, headers=get_headers(access_token), timeout=30)
        if response.status_code == 200: return response.json().get("data", [])
        return []
    except Exception: return []


def get_nearest_expiry(instrument_key: str, access_token: str, base_url: str = BASE_URL):
    url = f"{base_url}/option/contract"
    params = {"instrument_key": instrument_key}
    try:
        response = requests.get(url, params=params, headers=get_headers(access_token), timeout=20)
        if response.status_code == 200:
            contracts = response.json().get("data", [])
            if contracts:
                today = date.today().isoformat()
                expiries = sorted({str(c.get("expiry") or c.get("expiry_date")) for c in contracts if str(c.get("expiry") or c.get("expiry_date") or "") >= today})
                return expiries[0] if expiries else None
        return None
    except Exception: return None


def analyze_and_matrix(
    symbol_name: str,
    access_token: str,
    base_url: str = BASE_URL,
    instrument_key: str | None = None, # <-- Added fallback option to capture the frontend input argument safely
    **kwargs, # <-- Safeguards against any other unexpected keyword parameters passed from the UI
) -> dict:
    clean_symbol = symbol_name.upper().strip()
    if not clean_symbol: return {"error": "Enter a symbol."}
    if not access_token: return {"error": "No Upstox access token."}

    # Use frontend-passed key if available, otherwise resolve from local mapping list
    if not instrument_key:
        instrument_key = SYMBOL_MAP.get(clean_symbol)
        
    warning = None
    if not instrument_key:
        if "MCX" in clean_symbol: instrument_key = f"MCX_FO|{clean_symbol.replace('MCX', '').strip()}"
        else:
            instrument_key = f"NSE_INDEX|{clean_symbol}"
            warning = f"Assuming index: {instrument_key}"

    ltp = fetch_market_quote(instrument_key, access_token, base_url)
    if not ltp: return {"error": f"Failed to fetch live price for key: {instrument_key}"}

    # 1. Standard Risk Parameter Configuration Defaults
    if "NIFTY" in clean_symbol and "BANK" not in clean_symbol:
        sl_buffer, target_buffer, entry_chase_limit = 40.0, 150.0, 20.0  
    elif "BANKNIFTY" in clean_symbol:
        sl_buffer, target_buffer, entry_chase_limit = 100.0, 350.0, 50.0
    elif "CRUDEOIL" in clean_symbol:
        sl_buffer, target_buffer, entry_chase_limit = 30.0, 100.0, 15.0
    else:
        sl_buffer = round(ltp * 0.005, 1)
        target_buffer = round(ltp * 0.015, 1)
        entry_chase_limit = round(ltp * 0.002, 1)

    # 2. Extract Support & Resistance levels from Open Interest Data clusters
    resistance_level = None
    support_level = None
    pcr: float | str = "N/A"
    
    expiry = get_nearest_expiry(instrument_key, access_token, base_url) if "MCX" not in instrument_key else None
    if expiry:
        chain_data = fetch_option_chain(instrument_key, expiry, access_token, base_url)
        if chain_data:
            try:
                df = pd.DataFrame(chain_data)
                
                # Safely unpack nested call option values
                if "call_options" in df.columns:
                    df["call_oi"] = df["call_options"].apply(lambda x: x.get("market_data", {}).get("oi", 0) if pd.notnull(x) and isinstance(x, dict) else 0)
                else:
                    df["call_oi"] = 0
                    
                # Safely unpack nested put option values
                if "put_options" in df.columns:
                    df["put_oi"] = df["put_options"].apply(lambda x: x.get("market_data", {}).get("oi", 0) if pd.notnull(x) and isinstance(x, dict) else 0)
                else:
                    df["put_oi"] = 0

                # Calculate Global PCR
                total_call_oi = df["call_oi"].sum()
                total_put_oi = df["put_oi"].sum()
                if total_call_oi > 0: 
                    pcr = round(total_put_oi / total_call_oi, 2)

                # FIND TRUE CALL WALL (Resistance) -> Highest Call OI Strike
                if total_call_oi > 0:
                    max_call_idx = df["call_oi"].idxmax()
                    resistance_level = float(df.loc[max_call_idx, "strike_price"])
                    
                # FIND TRUE PUT FLOOR (Support) -> Highest Put OI Strike
                if total_put_oi > 0:
                    max_put_idx = df["put_oi"].idxmax()
                    support_level = float(df.loc[max_put_idx, "strike_price"])
            except Exception:
                pass

    # Safety Addon: Fallback to geometric brackets if Option Chain API fails
    if not resistance_level or not support_level:
        warning = "⚠️ Running on Chart Price Action Fallback (Option Chain data unavailable or timed out)."
        if "NIFTY" in clean_symbol and "BANK" not in clean_symbol:
            resistance_level = round(ltp + 100, 0)
            support_level = round(ltp - 100, 0)
        elif "BANKNIFTY" in clean_symbol:
            resistance_level = round(ltp + 250, 0)
            support_level = round(ltp - 250, 0)
        elif "CRUDEOIL" in clean_symbol:
            resistance_level = round(ltp + 80, 0)
            support_level = round(ltp - 80, 0)
        else:
            resistance_level = round(ltp * 1.01, 1)
            support_level = round(ltp * 0.99, 1)

    # 3. Micro-Trend Execution Windows Setup
    short_entry_start = support_level
    short_entry_end = round(support_level - entry_chase_limit, 1)
    short_target = round(support_level - target_buffer, 1)
    short_sl = round(support_level + sl_buffer, 1)

    long_entry_start = resistance_level
    long_entry_end = round(resistance_level + entry_chase_limit, 1)
    long_target = round(resistance_level + target_buffer, 1)
    long_sl = round(resistance_level - sl_buffer, 1)

    # 4. Narrative Synthesizer & Volumetric Filter Logic
    if pcr != "N/A":
        pcr_val = float(pcr)
        if pcr_val > 1.15:
            sentiment = "Strongly Bullish Bias"
            oi_story = "Put writers are building an aggressive structural baseline, dominating order flow dynamics and forcing prices upward."
        elif pcr_val < 0.85:
            sentiment = "Strongly Bearish Bias"
            oi_story = "Call writers are heavily stacking inventory limits overhead, capping recovery attempts and exerting downward pressure."
        else:
            sentiment = "Neutral / Rangebound Consolidation"
            oi_story = "Bears and Bulls are matched evenly across internal zones. Expect range bound behavior until volume expansion confirms a breakout direction."
    else:
        pcr_val = 1.0
        sentiment = "Volume Momentum Driven"
        oi_story = "Analyzing underlying momentum patterns via price action corridors."

    distance_to_ceiling = resistance_level - ltp
    distance_to_floor = ltp - support_level

    if distance_to_ceiling < distance_to_floor:
        proximity_note = f"The price action is tightening near the overhead Call Wall Resistance ceiling framework ({resistance_level:.1f}). Large institutions are actively defending this cluster; look for either a structural rejection trade or an intense short-covering rally if the cluster blows open."
    else:
        proximity_note = f"Price is resting closely to the structural Put Floor Support framework ({support_level:.1f}). Big money is attempting to hold this pocket; a distinct bounce confirms short-term intraday strength, while any sustained close below flags a sharp institutional sell-off cascade."

    # --- ADVANCED DIRECTIONAL RISK & VOLUMETRIC DIRECTIVE LOGIC ---
    room_to_upside = resistance_level - ltp
    room_to_downside = ltp - support_level

    if pcr_val < 0.85 and room_to_downside > room_to_upside:
        trade_bias_note = f"⚠️ WARNING: Despite the structural Support Floor sitting lower at {support_level:.1f}, the aggressive Bearish Bias (PCR: {pcr_val}) indicates overhead supply is actively forcing price compression."
        volume_directive = f"🛑 ACTION DIRECTIVE: AVOID chasing immediate longs. Enter a STRATEGIC SELL ONLY on a validated breakdown below {support_level:.1f} accompanied by an expansion in intraday option volume, targeting {short_target:.1f}."
    elif pcr_val > 1.15 and room_to_upside > room_to_downside:
        trade_bias_note = f"🚀 OPPORTUNITY: The strong Bullish Bias (PCR: {pcr_val}) combined with immediate proximity to structural floors suggests downside risk is highly limited."
        volume_directive = f"🟢 ACTION DIRECTIVE: Prepare to STRATEGIC BUY on a distinct technical reversal pattern above {support_level:.1f} or upon a clean breakout over {resistance_level:.1f} carrying high volume, targeting {long_target:.1f}."
    else:
        trade_bias_note = f"⚡ Execution Edge: System is currently pricing a balanced intraday corridor."
        volume_directive = f"🟡 ACTION DIRECTIVE: AVOID entering positions in the middle zone. Remain completely flat and wait patiently to execute trades strictly at the outer extreme triggers ({support_level:.1f} or {resistance_level:.1f})."

    market_summary_text = (
        f"### 🎯 Market Intelligence Narrative\n"
        f"**Current Trend Stance:** {sentiment}\n\n"
        f"**Institutional Structure:** The market is currently tracking at **{ltp:.2f}** with an options Put-Call Ratio (PCR) of **{pcr}**. "
        f"Institutions have established a firm intraday **Put Floor (Support) at {support_level:.1f}** and an overhead **Call Wall (Resistance) at {resistance_level:.1f}**. {oi_story}\n\n"
        f"**Tactical Overview:** {proximity_note}\n\n"
        f"{trade_bias_note}\n\n"
        f"{volume_directive}"
    )

    matrix_data = [
        {
            "Scenario": "Short Entry",
            "Price Action Trigger": f"Breakdown below {support_level}",
            "Execution Entry Range": f"{short_entry_start} down to {short_entry_end}",
            "OI Condition": "Increasing",
            "Tactical Plan": f"Target {short_target} | Stop Loss {short_sl}",
        },
        {
            "Scenario": "Long Entry",
            "Price Action Trigger": f"Breakout above {resistance_level}",
            "Execution Entry Range": f"{long_entry_start} up to {long_entry_end}",
            "OI Condition": "Increasing",
            "Tactical Plan": f"Target {long_target} | Stop Loss {long_sl}",
        }
    ]

    return {
        "symbol": clean_symbol,
        "instrument_key": instrument_key,
        "ltp": ltp,
        "pcr": pcr,
        "resistance_level": resistance_level,
        "support_level": support_level,
        "expiry": expiry,
        "is_commodity": "MCX" in instrument_key,
        "matrix_data": matrix_data,
        "market_summary": market_summary_text,
        "warning": warning,
    }