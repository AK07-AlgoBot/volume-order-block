#!/usr/bin/env python3
"""
Test MCX Order Placement - Confirm lot sizes by placing dummy orders
"""

import json
import requests
from pathlib import Path
from datetime import datetime
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.upstox.com/v2"
INTERVAL = "5minute"

MCX_SCRIPTS = {
    "CRUDE": {
        "instrument": "MCX_FO|472789",
        "lot_size": 100,
        "quantity": 1
    },
    "GOLDMINI": {
        "instrument": "MCX_FO|487665",
        "lot_size": 1,
        "quantity": 1
    },
    "SILVERMINI": {
        "instrument": "MCX_FO|457533",
        "lot_size": 5,
        "quantity": 1
    }
}

BOT_FILE = Path("trading_bot.py")


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


def fetch_ltp(access_token, instrument_key):
    """Fetch last traded price"""
    try:
        url = f"{API_BASE_URL}/historical-candle/intraday/{instrument_key}/{INTERVAL}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        payload = response.json()
        candles = payload.get("data", {}).get("candles", [])
        if not candles:
            return None
        return float(candles[-1][4])  # close price is at index 4
    except Exception as e:
        logger.error(f"Error fetching LTP for {instrument_key}: {e}")
        return None


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
            "product": "I",  # Intraday for MCX futures
            "validity": "DAY",
            "price": 0,
            "tag": "test_mcx",
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
            logger.info(f"✅ Order placed successfully!")
            logger.info(f"   Response: {data.get('data', {})}")
            return True
        else:
            logger.error(f"❌ Order failed: {data}")
            return False
    except Exception as e:
        logger.error(f"❌ Error placing order: {e}")
        return False


def main():
    print("\n" + "="*80)
    print("MCX DUMMY ORDER TEST - Confirm Lot Sizes")
    print("="*80)
    
    access_token = extract_access_token()
    if not access_token:
        print("❌ Could not read access token from trading_bot.py")
        return
    
    print(f"✅ Token loaded successfully\n")
    
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
        
        # Fetch LTP
        ltp = fetch_ltp(access_token, instrument_key)
        if ltp:
            print(f"✅ Current LTP: ₹{ltp:.2f}")
            estimated_margin = ltp * lot_size * quantity
            print(f"   Estimated margin required: ₹{estimated_margin:.2f}")
        else:
            print(f"⚠️  Could not fetch LTP")
        
        # Place order
        print(f"\nPlacing BUY order for {quantity} lot(s)...")
        success = place_order(access_token, instrument_key, quantity, "BUY")
        
        if success:
            print(f"✅ {script_name} order placed successfully!")
        else:
            print(f"❌ {script_name} order failed - check logs for details")
    
    print(f"\n{'='*80}")
    print("Test Complete - Check trading_bot.log for details")
    print("="*80 + "\n")


if __name__ == "__main__":
    main()
