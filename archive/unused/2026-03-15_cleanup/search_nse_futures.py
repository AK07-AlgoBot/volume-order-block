#!/usr/bin/env python3
"""
Search NSE instruments for active NIFTY and BANKNIFTY FUTURES contracts
"""

import requests
import json
import gzip

NSE_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

def fetch_nse_instruments():
    """Download and search NSE for NIFTY and BANKNIFTY FUTURES"""
    print("Fetching NSE instruments...")
    try:
        response = requests.get(NSE_URL, timeout=30)
        response.raise_for_status()
        decompressed = gzip.decompress(response.content)
        data = json.loads(decompressed)
        
        print(f"✅ Loaded {len(data)} NSE instruments\n")
        
        nifty_contracts = []
        banknifty_contracts = []
        
        for instrument in data:
            name = instrument.get("name", "").upper()
            trading_symbol = instrument.get("trading_symbol", "").upper()
            instrument_type = instrument.get("instrument_type", "").upper()
            instrument_key = instrument.get("instrument_key", "")
            segment = instrument.get("segment", "").upper()
            
            # Search for NIFTY FUTURES (NSE_FO segment)
            if "NIFTY" in trading_symbol and "FUT" in instrument_type and segment == "NSE_FO":
                if "50" in trading_symbol or "NIFTY 50" in name or "NIFTY 50" in trading_symbol:
                    nifty_contracts.append({
                        "instrument_key": instrument_key,
                        "name": instrument.get("name"),
                        "trading_symbol": instrument.get("trading_symbol"),
                        "instrument_type": instrument_type,
                        "lot_size": instrument.get("lot_size")
                    })
            
            # Search for BANKNIFTY FUTURES
            if "BANKNIFTY" in trading_symbol and "FUT" in instrument_type and segment == "NSE_FO":
                banknifty_contracts.append({
                    "instrument_key": instrument_key,
                    "name": instrument.get("name"),
                    "trading_symbol": instrument.get("trading_symbol"),
                    "instrument_type": instrument_type,
                    "lot_size": instrument.get("lot_size")
                })
        
        print("="*80)
        print("ACTIVE NIFTY 50 FUTURES CONTRACTS")
        print("="*80)
        for contract in nifty_contracts[:3]:  # Show first 3
            print(f"Name: {contract['name']}")
            print(f"Symbol: {contract['trading_symbol']}")
            print(f"Type: {contract['instrument_type']}")
            print(f"Lot Size: {contract['lot_size']}")
            print(f"Key: {contract['instrument_key']}")
            print()
        
        print("\n" + "="*80)
        print("ACTIVE BANKNIFTY FUTURES CONTRACTS")
        print("="*80)
        for contract in banknifty_contracts[:3]:  # Show first 3
            print(f"Name: {contract['name']}")
            print(f"Symbol: {contract['trading_symbol']}")
            print(f"Type: {contract['instrument_type']}")
            print(f"Lot Size: {contract['lot_size']}")
            print(f"Key: {contract['instrument_key']}")
            print()
        
        if nifty_contracts and banknifty_contracts:
            print("\n" + "="*80)
            print("RECOMMENDED KEYS TO USE:")
            print("="*80)
            print(f"NIFTY: {nifty_contracts[0]['instrument_key']}  (Lot: {nifty_contracts[0]['lot_size']})")
            print(f"BANKNIFTY: {banknifty_contracts[0]['instrument_key']}  (Lot: {banknifty_contracts[0]['lot_size']})")
        
        return nifty_contracts, banknifty_contracts
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return [], []

if __name__ == "__main__":
    fetch_nse_instruments()
