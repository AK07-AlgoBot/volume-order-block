#!/usr/bin/env python3
"""
Detailed MCX order test with error inspection
"""

import json
import requests
from pathlib import Path
from datetime import datetime
import logging

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.upstox.com/v2"
BOT_FILE = Path("trading_bot.py")

MCX_SCRIPTS = {
    "CRUDE": {
        "instrument": "MCX_FO|472789",
        "lot_size": 100,
        "quantity": 1
    },
    "GOLDMINI": {
        "instrument": "MCX_FO|472683",
        "lot_size": 1,
        "quantity": 1
    },
    "SILVERMINI": {
        "instrument": "MCX_FO|506661",
        "lot_size": 5,
        "quantity": 1
    }
}

def extract_access_token():
    """Extract access token from trading_bot.py"""
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

def place_order_detailed(access_token, instrument_key, quantity, transaction_type="BUY"):
    """Place an order with detailed error reporting"""
    try:
        url = "https://api-hft.upstox.com/v2/order/place"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        payload = {
            "quantity": quantity,
            "product": "I",  # Intraday
            "validity": "DAY",
            "price": 0,
            "tag": "test_mcx_detailed",
            "instrument_token": instrument_key,
            "order_type": "MARKET",
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False
        }
        
        print(f"\n📤 Sending request to {url}")
        print(f"📋 Payload: {json.dumps(payload, indent=2)}")
        
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        
        print(f"📥 Response Status: {response.status_code}")
        print(f"📥 Response Headers: {dict(response.headers)}")
        print(f"📥 Response Body: {response.text}")
        
        response.raise_for_status()
        data = response.json()
        
        if data.get('status') == 'success':
            print(f"✅ Order placed successfully! Order ID: {data.get('data', {}).get('order_id')}")
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
    print("DETAILED MCX ORDER TEST")
    print("="*80)
    
    access_token = extract_access_token()
    if not access_token:
        print("❌ Could not read access token from trading_bot.py")
        return
    
    print(f"✅ Token loaded\n")
    
    for script_name, script_config in MCX_SCRIPTS.items():
        instrument_key = script_config["instrument"]
        lot_size = script_config["lot_size"]
        quantity = script_config["quantity"]
        
        print(f"\n{'='*80}")
        print(f"Testing: {script_name}")
        print(f"  Instrument: {instrument_key}")
        print(f"  Lot Size: {lot_size}")
        print(f"  Quantity: {quantity}")
        print(f"{'='*80}")
        
        place_order_detailed(access_token, instrument_key, quantity, "BUY")

if __name__ == "__main__":
    main()
