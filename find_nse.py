import requests, gzip, json

NSE_URL = 'https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz'
response = requests.get(NSE_URL, timeout=30)
data = json.loads(gzip.decompress(response.content))

# Find all NIFTY futures
nifty_all = [i for i in data if 'NIFTY' in i.get('name','').upper() and 'FUT' in i.get('trading_symbol','').upper()]

print('='*80)
print('ALL NIFTY FUTURES CONTRACTS')
print('='*80)
for n in nifty_all[:15]:
    print(f"{n['name']:20} | {n['trading_symbol']:30} | Lot: {n['lot_size']:3} | {n['instrument_key']}")

print('\n' + '='*80)
print('MOST LIKELY CANDIDATES (Based on Upstox terminal):')
print('='*80)
print("NIFTY (Lot 65): Check above for NIFTY with lot_size=65")
print("BANKNIFTY (Lot 30): NSE_FO|51701")

