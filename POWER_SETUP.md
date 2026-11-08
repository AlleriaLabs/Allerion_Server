# ALLERION ServerMonitor setup

## Deploy / update
1. Back up the existing `data/` directory before updating; it contains energy history and power configuration.
2. Copy `.env.example` to `.env`, set a strong random `TOKEN`, and do not share or commit `.env`.
3. Build and start with `docker compose up -d --build`.
4. The monitoring API listens on port 8081. Keep it on a trusted LAN or place it behind HTTPS for remote access.

## Advanced infrastructure
- Docker containers are read through a loopback-only Docker socket proxy. The proxy disables POST operations and only exposes container endpoints. Do not expose port 2375 beyond localhost.
- Process monitoring returns PID, process name, user, CPU and resident memory only. It does not expose command-line arguments or environment variables.
- LAN inventory is passive ARP/neighbor-cache discovery. It does not actively scan the subnet. Enable with `ENABLE_LAN_DISCOVERY=1`.
- Service checks can be configured with `SERVICE_CHECKS`, for example `Jellyfin=http://127.0.0.1:8096/health;CasaOS=http://127.0.0.1/`. Only configure trusted endpoints.
- Network diagnostics accept private IPv4 targets only and test TCP ports 443, 80, 22 and 8080. Packet loss is not measured.
- Webhook alerts require an HTTPS URL in `ALERT_WEBHOOK_URL`; optional `ALERT_WEBHOOK_SECRET` is sent as `X-Webhook-Secret`. Telegram alerts require `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. High CPU/RAM/temperature/disk threshold crossings and recovery transitions are sent to configured destinations. Defaults are CPU 90%, RAM 90%, temperature 85 °C and disk 92%; override with `ALERT_CPU_THRESHOLD`, `ALERT_RAM_THRESHOLD`, `ALERT_TEMP_THRESHOLD` and `ALERT_DISK_THRESHOLD`. Never enter these secrets in the Android client.

## Power reading
Open ALLERION > server > Power tab > Configure power source, choose the plug, test, and save. Power config and history remain in the persistent `data/` folder.
Existing Tuya / Smart Life, Tapo, Shelly and Tasmota options remain supported. Keep plug keys and account passwords in the persistent server-side configuration only.

## Energy forecast / source breakdown
New deployments record daily kWh by source (smart plug, CPU/RAPL, or CPU + base estimate). Existing energy totals are retained; old dates are not retroactively attributed to a source. Forecasts use a simple arithmetic mean of recorded daily totals and are estimates, not guarantees.

## Security
- API requests require the `X-Token` header. Query-string token authentication has been removed.
- `TOKEN` is mandatory unless `ALLOW_INSECURE_LOCAL=1` is explicitly set for isolated local testing only.
- Power configuration containing plug credentials is written with restrictive file permissions.
- Do not expose port 8081 or the Docker proxy directly to the public internet. Use HTTPS/reverse proxy for remote access.
