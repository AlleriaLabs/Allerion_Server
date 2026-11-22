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
3. Run `docker compose up -d --build` (the backend service/container is named `allerion-server`).
4. The API listens on port `8081` in the host network namespace.

Back up and preserve the `data/` directory on the server. This repository intentionally excludes runtime energy history and saved power configuration.

## Account sign-in
- New Android clients can use individual Allerion usernames and passwords. Passwords are stored as PBKDF2-HMAC-SHA256 hashes; the server never stores the raw password.
- On the first connection, the first administrator account is created using the existing `.env` `TOKEN` once as a bootstrap credential. Choose a username and a password of at least 12 characters.
- After setup, users sign in with their own credentials. The administrator can create additional accounts from the Server Workspace account panel.
- Password accounts and hashed bearer-session records are stored in `data/auth.sqlite3`; preserve and back up the existing `data/` directory.
- Existing `X-Token` clients remain supported during migration. New Android clients store expiring bearer sessions in Android Keystore-backed encrypted storage.

## Security
- Keep `.env`, account database backups and power-device credentials private.
- Do not expose port 8081 or the Docker socket proxy to the public internet.
- Docker access is through a loopback-only proxy with write operations disabled.
- The current backend listener is HTTP. Password sign-in must be limited to a trusted private LAN until HTTPS/reverse proxy is configured. Never send account passwords over an untrusted network.

See [API_V2.md](API_V2.md) and [POWER_SETUP.md](POWER_SETUP.md) for endpoint and deployment details.
