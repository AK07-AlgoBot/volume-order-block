#!/usr/bin/env python3
"""
Fetch and parse MCX instrument keys for CRUDE, GOLDMINI, SILVERMINI
"""

import requests
import json
import gzip
from io import BytesIO

MCX_URL = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"

def fetch_mcx_instruments():
    """Download and decompress MCX instruments"""
    print("Fetching MCX instruments from Upstox...")
    try:
        response = requests.get(MCX_URL, timeout=30)
        response.raise_for_status()
        
        # Decompress gzip
        decompressed = gzip.decompress(response.content)
        data = json.loads(decompressed)
        
        print(f"✅ Loaded {len(data)} MCX instruments\n")
        
        # Search for CRUDE, GOLDMINI, SILVERMINI
        search_terms = {
            "CRUDE": ["crude", "crode01", "naturalgas"],
            "GOLDMINI": ["gold", "goldmini", "goldpetal"],
            "SILVERMINI": ["silver", "silvermini"]
        }
        
        found = {}
        
        for instrument in data:
            name = instrument.get("name", "").lower()
            trading_symbol = instrument.get("trading_symbol", "").lower()
            instrument_key = instrument.get("instrument_key", "")
            
            for search_name, keywords in search_terms.items():
                if search_name not in found:  # Only get first match for each
                    for keyword in keywords:
                        if keyword in name or keyword in trading_symbol:
                            found[search_name] = {
                                "instrument_key": instrument_key,
                                "name": instrument.get("name", ""),
                                "trading_symbol": instrument.get("trading_symbol", ""),
                                "lot_size": instrument.get("lot_size", ""),
                                "exchange_token": instrument.get("exchange_token", "")
                            }
                            print(f"Found {search_name}:")
                            print(f"  Name: {instrument.get('name')}")
                            print(f"  Trading Symbol: {instrument.get('trading_symbol')}")
                            print(f"  Instrument Key: {instrument_key}")
                            print(f"  Lot Size: {instrument.get('lot_size')}")
                            print()
                            break
        
        return found
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return {}

if __name__ == "__main__":
    found = fetch_mcx_instruments()
    if found:
        print("\n" + "="*80)
        print("UPDATE YOUR SCRIPTS WITH THESE INSTRUMENT KEYS:")
        print("="*80)
        for key, value in found.items():
            print(f"{key}: {value['instrument_key']}")
