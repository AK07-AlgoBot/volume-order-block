#!/usr/bin/env python3
"""
Search MCX instruments for active GOLD and SILVER FUTURES (not options)
"""

import requests
import json
import gzip
from io import BytesIO

MCX_URL = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"

def fetch_mcx_instruments():
    """Download and search MCX for GOLD and SILVER FUTURES"""
    print("Fetching MCX instruments...")
    try:
        response = requests.get(MCX_URL, timeout=30)
        response.raise_for_status()
        decompressed = gzip.decompress(response.content)
        data = json.loads(decompressed)
        
        print(f"✅ Loaded {len(data)} MCX instruments\n")
        
        gold_contracts = []
        silver_contracts = []
        
        for instrument in data:
            name = instrument.get("name", "").upper()
            trading_symbol = instrument.get("trading_symbol", "").upper()
            instrument_type = instrument.get("instrument_type", "").upper()
            instrument_key = instrument.get("instrument_key", "")
            
            # Search for GOLD FUTURES
            if "GOLD" in name and "FUT" in trading_symbol and "FUT" in instrument_type:
                gold_contracts.append({
                    "instrument_key": instrument_key,
                    "name": instrument.get("name"),
                    "trading_symbol": instrument.get("trading_symbol"),
                    "instrument_type": instrument_type,
                    "lot_size": instrument.get("lot_size")
                })
            
            # Search for SILVER FUTURES  
            if "SILVER" in name and "FUT" in trading_symbol and "FUT" in instrument_type:
                silver_contracts.append({
                    "instrument_key": instrument_key,
                    "name": instrument.get("name"),
                    "trading_symbol": instrument.get("trading_symbol"),
                    "instrument_type": instrument_type,
                    "lot_size": instrument.get("lot_size")
                })
        
        print("="*80)
        print("ACTIVE GOLD FUTURES CONTRACTS")
        print("="*80)
        for contract in gold_contracts[:5]:  # Show first 5
            print(f"Name: {contract['name']}")
            print(f"Symbol: {contract['trading_symbol']}")
            print(f"Type: {contract['instrument_type']}")
            print(f"Lot Size: {contract['lot_size']}")
            print(f"Key: {contract['instrument_key']}")
            print()
        
        print("\n" + "="*80)
        print("ACTIVE SILVER FUTURES CONTRACTS")
        print("="*80)
        for contract in silver_contracts[:5]:  # Show first 5
            print(f"Name: {contract['name']}")
            print(f"Symbol: {contract['trading_symbol']}")
            print(f"Type: {contract['instrument_type']}")
            print(f"Lot Size: {contract['lot_size']}")
            print(f"Key: {contract['instrument_key']}")
            print()
        
        if gold_contracts and silver_contracts:
            print("\n" + "="*80)
            print("RECOMMENDED KEYS TO USE:")
            print("="*80)
            print(f"GOLDMINI: {gold_contracts[0]['instrument_key']}  (Lot: {gold_contracts[0]['lot_size']})")
            print(f"SILVERMINI: {silver_contracts[0]['instrument_key']}  (Lot: {silver_contracts[0]['lot_size']})")
        
        return gold_contracts, silver_contracts
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return [], []

if __name__ == "__main__":
    fetch_mcx_instruments()
