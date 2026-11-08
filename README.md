# Allerion Server

Python backend for the Allerion home/server monitoring Android app.

## Included
- Host CPU, memory, temperature, disk, process and network monitoring
- Docker container inventory and point-in-time resource metrics through a read-only socket proxy
- Configurable service health checks
- Passive LAN neighbor inventory and private-IP TCP diagnostics
- Energy history, source breakdown and estimated forecast endpoints
- Webhook and Telegram alert integrations
- Existing power-monitoring integrations (Tuya/Smart Life, Tapo, Shelly and Tasmota)

## Quick start (Docker Compose)
1. Copy `.env.example` to `.env`.
2. Set `TOKEN` to a long, unique random secret. Never commit `.env`.
3. Run `docker compose up -d --build`.
4. The API listens on port `8081` in the host network namespace.

Back up and preserve the `data/` directory on the server. This repository intentionally excludes runtime energy history and saved power configuration.

## Security
- All `/api/*` routes require the `X-Token` header.
- Do not expose port 8081 or the Docker socket proxy to the public internet.
- Docker access is through a loopback-only proxy with write operations disabled.
- Keep `.env` and power-device credentials private.
- Use HTTPS/reverse proxy for access outside a trusted LAN.

See [API_V2.md](API_V2.md) and [POWER_SETUP.md](POWER_SETUP.md) for endpoint and deployment details.
