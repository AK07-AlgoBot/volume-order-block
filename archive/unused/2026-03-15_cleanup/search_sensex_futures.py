#!/usr/bin/env python3
"""
Search BSE instruments for active SENSEX FUTURES contracts
"""

import requests
import json
import gzip

BSE_URL = "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz"

def fetch_bse_instruments():
    """Download and search BSE for SENSEX FUTURES"""
    print("Fetching BSE instruments...")
    try:
        response = requests.get(BSE_URL, timeout=30)
        response.raise_for_status()
        decompressed = gzip.decompress(response.content)
        data = json.loads(decompressed)
        
        print(f"✅ Loaded {len(data)} BSE instruments\n")
        
        sensex_contracts = []
        
        for instrument in data:
            name = instrument.get("name", "").upper()
            trading_symbol = instrument.get("trading_symbol", "").upper()
            instrument_type = instrument.get("instrument_type", "").upper()
            instrument_key = instrument.get("instrument_key", "")
            segment = instrument.get("segment", "").upper()
            
            # Search for SENSEX FUTURES
            if "SENSEX" in trading_symbol and "FUT" in instrument_type and segment == "BSE_FO":
                sensex_contracts.append({
                    "instrument_key": instrument_key,
                    "name": instrument.get("name"),
                    "trading_symbol": instrument.get("trading_symbol"),
                    "instrument_type": instrument_type,
                    "lot_size": instrument.get("lot_size")
                })
        
        print("="*80)
        print("ACTIVE SENSEX FUTURES CONTRACTS")
        print("="*80)
        for contract in sensex_contracts[:5]:  # Show first 5
            print(f"Name: {contract['name']}")
            print(f"Symbol: {contract['trading_symbol']}")
            print(f"Type: {contract['instrument_type']}")
            print(f"Lot Size: {contract['lot_size']}")
            print(f"Key: {contract['instrument_key']}")
            print()
        
        if sensex_contracts:
            print("\n" + "="*80)
            print("RECOMMENDED KEY TO USE:")
            print("="*80)
            print(f"SENSEX: {sensex_contracts[0]['instrument_key']}  (Lot: {sensex_contracts[0]['lot_size']})")
        
        return sensex_contracts
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return []

if __name__ == "__main__":
    fetch_bse_instruments()
