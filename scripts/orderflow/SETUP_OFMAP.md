# OrderFlowMap on ak07.in (Upstox — no OpenAlgo, no local IP)

## Use this (production)

1. Deploy / pull `AK07-Model` and rebuild API container.
2. Update host nginx from `configs/host-nginx-ak07.conf.example` (needs `/ofmap/` + WS upgrade on `/api/`).
3. Open:

**https://ak07.in/ofmap/**

4. Click **Live** → **Connect**  
   - WS URL auto-fills: `wss://ak07.in/api/ofmap/ws`  
   - Symbol: `NIFTY` · Exchange: `NFO`  
   - API key: leave `ak07` (optional if `AK07_OFMAP_API_KEY` unset on server)  
5. Upstox token comes from server file:  
   `src/server/data/users/AK07/upstox_credentials.json`  
   — same login AK07 already uses. No second Upstox login in the browser.

### Health

```bash
curl -s https://ak07.in/api/ofmap/health
curl -s https://ak07.in/api/health
```

### Nginx (required once)

Copy the `/ofmap/` block and `Upgrade` headers under `/api/` from  
`configs/host-nginx-ak07.conf.example`, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### Docker rebuild

```bash
cd ~/volume-order-block
git pull origin AK07-Model
docker compose -p ak07 -f configs/docker-compose.yml up -d --build api
```

Env (optional):

| Var | Default | Meaning |
|-----|---------|---------|
| `AK07_OFMAP_USER` | `AK07` | Credentials bucket |
| `AK07_OFMAP_API_KEY` | empty = accept any | Browser authenticate key |

## Local-only bridge (optional)

Still available if you are not using the domain:

```powershell
python scripts/orderflow/upstox_ofmap_bridge.py --port 8766 --user AK07 --api-key ak07
python -m http.server 7890 --directory scripts/orderflow/OrderFlowMap
```

Prefer **https://ak07.in/ofmap/** instead.
