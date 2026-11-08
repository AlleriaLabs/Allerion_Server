# ServerMonitor API v2

All `/api/*` routes require the `X-Token` header. Query-string tokens are deliberately not accepted because URLs are commonly retained in logs, browser history and referrers.

## Capability discovery
- `GET /api/capabilities`

## Host inventory
- `GET /api/processes?limit=30` — bounded process list (PID, name, username, CPU %, resident memory bytes). Command-line arguments and environment variables are never returned.
- `GET /api/containers` — Docker container list, requires the read-only Docker socket proxy.
- `GET /api/containers/{id}/stats` — point-in-time CPU and memory usage.
- `GET /api/services` — configured checks from `SERVICE_CHECKS`, formatted `name=https://host/health;name2=http://host/health`.

## LAN
- `GET /api/network/devices` — passive ARP / neighbor-cache inventory. Disabled unless `ENABLE_LAN_DISCOVERY=1`.
- `GET /api/network/topology` — passive inventory nodes; links are intentionally empty because an ARP table cannot prove physical topology.
- `GET /api/network/diagnostics?target=192.168.1.10` — TCP connect checks on ports 443, 80, 22 and 8080. Only private IPv4 addresses are accepted. Packet loss is not claimed or inferred.

## Energy
- `GET /api/energy` — backward-compatible daily totals.
- `GET /api/energy/breakdown` — daily totals grouped by the source that supplied the reading. New source-attribution history begins after this version is deployed; older days remain unattributed.
- `GET /api/energy/forecast?days=30` — simple arithmetic-mean estimate from recorded daily totals. Requires at least three observations and is explicitly marked as an estimate.

## Integrations
- `GET /api/integrations` — configuration state only, no secrets.
- `POST /api/integrations/webhook/test` — sends a test event to an HTTPS endpoint configured with `ALERT_WEBHOOK_URL`; optional `ALERT_WEBHOOK_SECRET` is sent as `X-Webhook-Secret`.
- `POST /api/integrations/telegram/test` — sends a test message using `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- Metric alert transitions (CPU, RAM, temperature, disk) are delivered to configured webhook and/or Telegram destinations, including recovery transitions. Thresholds are set by backend environment variables. The app cannot read or set integration secrets.

## Deployment notes
1. Copy `.env.example` to `.env`, set `TOKEN` to a long random value, and keep `.env` private.
2. Build and start using `docker compose up -d --build`.
3. The Docker socket is exposed only through `tecnativa/docker-socket-proxy`, bound to host loopback, with `POST=0` and container API access enabled. Do not publish port 2375 to the LAN or internet.
4. LAN discovery reads the host's neighbor cache only. It does not perform active subnet scanning.
5. Service checks are admin-configured URLs. Only configure trusted local endpoints.
6. Put the service behind HTTPS if accessed outside a trusted LAN. The API token is a bearer credential; do not expose plain HTTP to the public internet.

Existing `/api/stats`, `/api/history`, `/api/energy`, `/api/power/config`, `/api/power/test` and `/api/speedtest` behavior is retained for the current Android client.
