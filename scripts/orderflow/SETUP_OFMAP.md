# OrderFlowMap + Upstox (no OpenAlgo)

Live Bookmap-style UI driven by **your Upstox V3 `full` WebSocket**, bridged into
OrderFlowMap's Live protocol.

| Piece | Port | Role |
|-------|------|------|
| OrderFlowMap UI | **7890** | Browser heatmap |
| Upstox bridge | **8766** | `ws://…:8766` (8765 = AK07 MCP — do not reuse) |

## Local (Windows)

```powershell
# Terminal 1 — bridge (uses src/server/data/users/AK07/upstox_credentials.json)
python scripts/orderflow/upstox_ofmap_bridge.py --port 8766 --user AK07 --api-key ak07

# Terminal 2 — UI
cd scripts/orderflow/OrderFlowMap
python -m http.server 7890
```

Open http://localhost:7890 → **Live** → Connect:

- WS URL: `ws://127.0.0.1:8766`
- API key: `ak07`
- Symbol: `NIFTY` or `BANKNIFTY`
- Exchange: `NFO`
- Tick: `0.05`

## Server (after git pull)

```bash
cd /path/to/volume-order-block
git pull origin AK07-Model
# ensure Upstox token is valid for today
chmod +x scripts/orderflow/run_ofmap.sh
./scripts/orderflow/run_ofmap.sh
```

Or systemd-style two processes:

```bash
nohup python3 scripts/orderflow/upstox_ofmap_bridge.py --host 0.0.0.0 --port 8766 --user AK07 --api-key ak07 \
  > /tmp/ofmap-bridge.log 2>&1 &

nohup python3 -m http.server 7890 --directory scripts/orderflow/OrderFlowMap \
  > /tmp/ofmap-ui.log 2>&1 &
```

From a browser on the server box (or tunnel): `http://SERVER_IP:7890/`  
If the UI is remote, set WS URL to `ws://SERVER_IP:8766` (open firewall / SSH tunnel as needed).

## Notes

- **Valid Upstox access token required** for live ticks (same daily token AK07 uses).
- Symbol resolve works even with a stale token via the public instrument master refresh.
- Market hours: depth/trades are live in session; after hours you may only see thin/stale books.
- Needs `upstox-python-sdk` + `websockets` (already in the AK07 env).
- Does **not** place orders — visual only.
- Trading engines keep using `ltpc` via `upstox_feed.py`; this bridge is a separate `full` connection.

### Local test result (2026-09-03)

- Bridge auth + subscribe OK (`NIFTY → NSE_FO|68407`)
- Live ticks blocked locally by **expired Upstox token (401)** — retest tomorrow on server with fresh token
