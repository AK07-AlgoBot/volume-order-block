#!/usr/bin/env python3
"""
Test GOLD and SILVER with LIMIT orders instead of MARKET
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

MCX_SCRIPTS = {
    "GOLDMINI": {
        "instrument": "MCX_FO|472683",
        "name": "Gold Mini",
    },
    "SILVERMINI": {
        "instrument": "MCX_FO|506661",
        "name": "Silver Mini",
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

def fetch_ltp(access_token, instrument_key):
    """Fetch LTP via v3 quote API (which might work better)"""
    try:
        url = f"https://api.upstox.com/v3/market-quote/ltp?instrument_key={instrument_key}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get('status') == 'success' and 'data' in data:
            for key, val in data['data'].items():
                ltp = val.get('last_price')
                if ltp:
                    return float(ltp)
        return None
    except Exception as e:
        print(f"Error fetching LTP: {e}")
        return None

def place_limit_order(access_token, instrument_key, quantity, price, transaction_type="BUY"):
    """Place LIMIT order instead of MARKET"""
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
            "price": price,  # LIMIT price
            "tag": "test_gold_silver",
            "instrument_token": instrument_key,
            "order_type": "LIMIT",  # Use LIMIT instead of MARKET
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False
        }
        
        print(f"\n📤 Placing LIMIT order at ₹{price}")
        print(f"📋 Payload: {json.dumps(payload, indent=2)}")
        
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get('status') == 'success':
            order_id = data.get('data', {}).get('order_id')
            print(f"✅ LIMIT order placed! Order ID: {order_id}")
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
    print("GOLD & SILVER LIMIT ORDER TEST")
    print("="*80)
    
    access_token = extract_access_token()
    if not access_token:
        print("❌ Could not read access token")
        return
    
    print(f"✅ Token loaded\n")
    
    for script_name, config in MCX_SCRIPTS.items():
        instrument_key = config["instrument"]
        print(f"\n{'='*80}")
        print(f"Testing: {script_name} ({config['name']})")
        print(f"  Instrument: {instrument_key}")
        print(f"{'='*80}")
        
        # Fetch LTP
        ltp = fetch_ltp(access_token, instrument_key)
        if ltp:
            print(f"✅ Last Traded Price: ₹{ltp:.2f}")
            # Place order at LTP (as if market price)
            place_limit_order(access_token, instrument_key, 1, ltp, "BUY")
        else:
            print(f"⚠️  Could not fetch LTP, trying with estimated price")
            # Try with a reasonable estimated price
            place_limit_order(access_token, instrument_key, 1, 100, "BUY")

if __name__ == "__main__":
    main()
