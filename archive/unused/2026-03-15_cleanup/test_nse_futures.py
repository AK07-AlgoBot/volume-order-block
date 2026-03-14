#!/usr/bin/env python3
"""
Test NSE futures orders with actual contract tokens
"""

import json
import requests
from pathlib import Path
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BOT_FILE = Path("trading_bot.py")

NSE_SCRIPTS = {
    "NIFTY": {
        "instrument": "NSE_FO|51715",
        "lot_size": 25,
        "quantity": 1
    },
    "BANKNIFTY": {
        "instrument": "NSE_FO|51701",
        "lot_size": 30,
        "quantity": 1
    }
}

def extract_access_token():
    if not BOT_FILE.exists():
        return None
    content = BOT_FILE.read_text(encoding="utf-8", errors="ignore")
    marker = '"access_token":'
    idx = content.find(marker)
    if idx == -1:
        return None
    start = content.find('"', idx + len(marker))
    if start == -1:
        return None
    end = content.find('"', start + 1)
    if end == -1:
        return None
    token = content[start + 1:end].strip()
    return token or None

def place_order(access_token, instrument_key, quantity, transaction_type="BUY"):
    """Place an order on Upstox"""
    try:
        url = "https://api-hft.upstox.com/v2/order/place"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        payload = {
            "quantity": quantity,
            "product": "I",
            "validity": "DAY",
            "price": 0,
            "tag": "test_nse_futures",
            "instrument_token": instrument_key,
            "order_type": "MARKET",
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False
        }
        
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get('status') == 'success':
            order_id = data.get('data', {}).get('order_id')
            print(f"✅ Order placed! Order ID: {order_id}")
            return True
        else:
            print(f"❌ Order failed: {data}")
            return False
    except requests.exceptions.HTTPError as e:
        print(f"❌ HTTP Error: {e}")
        print(f"   Response: {e.response.text}")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def main():
    print("\n" + "="*80)
    print("NSE INDEX FUTURES ORDER TEST")
    print("="*80)
    
    access_token = extract_access_token()
    if not access_token:
        print("❌ Could not read access token")
        return
    
    print(f"✅ Token loaded\n")
    
    for script_name, config in NSE_SCRIPTS.items():
        instrument_key = config["instrument"]
        lot_size = config["lot_size"]
        quantity = config["quantity"]
        
        print(f"\n{'='*80}")
        print(f"Testing: {script_name}")
        print(f"  Instrument: {instrument_key}")
        print(f"  Lot Size: {lot_size}")
        print(f"  Quantity: {quantity}")
        print(f"{'='*80}")
        
        print(f"Placing BUY order for {quantity} lot(s)...")
        success = place_order(access_token, instrument_key, quantity, "BUY")
        
        if success:
            print(f"✅ {script_name} order successful!")
        else:
            print(f"❌ {script_name} order failed")

if __name__ == "__main__":
    main()
