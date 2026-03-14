"""Test different data fetching methods"""
import requests
from datetime import datetime, timedelta

ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI2RUJBVTQiLCJqdGkiOiI2OWIwZGQ2ZWY4NTI4NzE1NGUyYTZmOTgiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlhdCI6MTc3MzE5ODcwMiwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxNzczMjY2NDAwfQ.VpDPbPQBIbjN-4WQKNM18cskWfLTQrdUcCuBZUzPJ20"

headers = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json"
}

# Test instruments
instruments = {
    "NIFTY_FUT": "NSE_FO|51714",
    "BANKNIFTY_FUT": "NSE_FO|51701", 
    "SENSEX_FUT": "BSE_FO|825565",
    "CRUDE": "MCX_FO|472789",
}

# Test different interval formats
intervals = ["1minute", "5minute", "1m", "5m", "I1", "I5"]

print("Testing Historical Candles API:\n")
print("="*100)

for name, instrument in instruments.items():
    print(f"\n{name} ({instrument}):")
    for interval in intervals:
        to_date = datetime.now().strftime('%Y-%m-%d')
        from_date = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
        
        url = f"https://api.upstox.com/v2/historical-candle/{instrument}/{interval}/{to_date}/{from_date}"
        
        try:
            response = requests.get(url, headers=headers, timeout=5)
            if response.status_code == 200:
                data = response.json()
                candles = data.get('data', {}).get('candles', [])
                print(f"  ✓ {interval}: SUCCESS ({len(candles)} candles)")
                break  # Found working interval
            else:
                print(f"  ✗ {interval}: {response.status_code}")
        except Exception as e:
            print(f"  ✗ {interval}: ERROR - {str(e)[:50]}")
    
print("\n" + "="*100)
print("\nTesting Intraday Candles API:\n")
print("="*100)

for name, instrument in instruments.items():
    print(f"\n{name} ({instrument}):")
    for interval in intervals:
        url = f"https://api.upstox.com/v2/historical-candle/intraday/{instrument}/{interval}"
        
        try:
            response = requests.get(url, headers=headers, timeout=5)
            if response.status_code == 200:
                data = response.json()
                candles = data.get('data', {}).get('candles', [])
                print(f"  ✓ {interval}: SUCCESS ({len(candles)} candles)")
                break  # Found working interval
            else:
                print(f"  ✗ {interval}: {response.status_code}")
        except Exception as e:
            print(f"  ✗ {interval}: ERROR - {str(e)[:50]}")

print("\n" + "="*100)
print("\nTesting Market Quotes API:\n")
print("="*100)

for name, instrument in instruments.items():
    url = f"https://api.upstox.com/v2/market-quote/quotes?symbol={instrument}"
    
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            print(f"  ✓ {name}: SUCCESS")
            if 'data' in data and instrument in data['data']:
                ltp = data['data'][instrument].get('last_price', 'N/A')
                print(f"    LTP: {ltp}")
        else:
            print(f"  ✗ {name}: {response.status_code}")
    except Exception as e:
        print(f"  ✗ {name}: ERROR - {str(e)[:50]}")

print("\n" + "="*100)
