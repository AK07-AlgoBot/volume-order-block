#!/usr/bin/env bash
# Run OrderFlowMap UI + Upstox full-feed bridge on the AK07 server.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

PORT_UI="${OFMAP_UI_PORT:-7890}"
PORT_WS="${OFMAP_WS_PORT:-8766}"
USER_BUCKET="${OFMAP_USER:-AK07}"

echo "Starting Upstox→OrderFlowMap bridge on :$PORT_WS (user=$USER_BUCKET)"
python3 scripts/orderflow/upstox_ofmap_bridge.py \
  --host 0.0.0.0 \
  --port "$PORT_WS" \
  --user "$USER_BUCKET" \
  --api-key ak07 &
BRIDGE_PID=$!

cleanup() { kill "$BRIDGE_PID" 2>/dev/null || true; }
trap cleanup EXIT

echo "Serving OrderFlowMap UI on :$PORT_UI"
echo "  Open: http://SERVER_IP:$PORT_UI/"
echo "  Live → WS ws://127.0.0.1:$PORT_WS  (or ws://SERVER_IP:$PORT_WS from remote)"
echo "  API key: ak07   Symbol: NIFTY   Exchange: NFO   Tick: 0.05"
cd scripts/orderflow/OrderFlowMap
python3 -m http.server "$PORT_UI"
