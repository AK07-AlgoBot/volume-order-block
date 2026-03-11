"""Test different Upstox data endpoints to see which ones work"""
import requests
from datetime import datetime, timedelta

ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI2RUJBVTQiLCJqdGkiOiI2OWIwZGQ2ZWY4NTI4NzE1NGUyYTZmOTgiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlhdCI6MTc3MzE5ODcwMiwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxNzczMjY2NDAwfQ.VpDPbPQBIbjN-4WQKNM18cskWfLTQrdUcCuBZUzPJ20"
BASE_URL = "https://api.upstox.com/v2"

headers = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json"
}

# Test different instrument formats
test_instruments = [
    ("NIFTY Index", "NSE_INDEX|Nifty50"),
    ("NIFTY Index (with space)", "NSE_INDEX|Nifty 50"),
    ("NIFTY Futures", "NSE_FO|51714"),
    ("BANKNIFTY Index", "NSE_INDEX|NiftyBank"),
    ("BANKNIFTY Index (with space)", "NSE_INDEX|Nifty Bank"),
    ("BANKNIFTY Futures", "NSE_FO|51701"),
    ("SENSEX Index", "BSE_INDEX|SENSEX"),
    ("SENSEX Futures", "BSE_FO|825565"),
    ("CRUDE Futures", "MCX_FO|472789"),
]

print("="*80)
print("TESTING MARKET QUOTE ENDPOINT")
print("="*80)

for name, instrument in test_instruments:
    try:
        url = f"{BASE_URL}/market-quote/quotes?instrument_key={instrument}"
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'success':
                quote_data = data.get('data', {}).get(instrument, {})
                ltp = quote_data.get('last_price', 'N/A')
                print(f"✓ {name:30} | {instrument:30} | LTP: {ltp}")
            else:
                print(f"✗ {name:30} | {instrument:30} | Error: {data.get('message', 'Unknown')}")
        else:
            print(f"✗ {name:30} | {instrument:30} | HTTP {response.status_code}")
    except Exception as e:
        print(f"✗ {name:30} | {instrument:30} | Exception: {e}")

print("\n" + "="*80)
print("TESTING INTRADAY CANDLE ENDPOINT (1minute)")
print("="*80)

for name, instrument in test_instruments:
    try:
        url = f"{BASE_URL}/historical-candle/intraday/{instrument}/1minute"
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'success':
                candles = data.get('data', {}).get('candles', [])
                print(f"✓ {name:30} | {instrument:30} | Candles: {len(candles)}")
            else:
                print(f"✗ {name:30} | {instrument:30} | Error: {data.get('message', 'Unknown')}")
        else:
            print(f"✗ {name:30} | {instrument:30} | HTTP {response.status_code}")
    except Exception as e:
        print(f"✗ {name:30} | {instrument:30} | Exception: {e}")

print("\n" + "="*80)
print("TESTING HISTORICAL CANDLE ENDPOINT (1minute, last 7 days)")
print("="*80)

to_date = datetime.now().strftime("%Y-%m-%d")
from_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

for name, instrument in test_instruments[:3]:  # Test just first 3
    try:
        url = f"{BASE_URL}/historical-candle/{instrument}/1minute/{to_date}/{from_date}"
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'success':
                candles = data.get('data', {}).get('candles', [])
                print(f"✓ {name:30} | {instrument:30} | Candles: {len(candles)}")
            else:
                print(f"✗ {name:30} | {instrument:30} | Error: {data.get('message', 'Unknown')}")
        else:
            print(f"✗ {name:30} | {instrument:30} | HTTP {response.status_code}")
    except Exception as e:
        print(f"✗ {name:30} | {instrument:30} | Exception: {e}")
