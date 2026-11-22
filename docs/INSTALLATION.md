# Allerion Server — Self-hosted deployment

Allerion runs on infrastructure you control. Monitoring data, accounts and integration credentials stay on your server. No Alleria cloud account or subscription is required.

## Supported first-release platform

- Linux x86_64 and ARM64 (aarch64)
- Docker Engine and Docker Compose v2
- CasaOS, Debian 12/13, Ubuntu Server and Raspberry Pi OS 64-bit
- LAN access; remote access requires HTTPS through a reverse proxy or a VPN

Windows/macOS and non-Docker deployments are not supported by this installer yet.

## Install

Install Docker Engine, Docker Compose v2, Git and OpenSSL first. Then run:

    curl -fsSL https://raw.githubusercontent.com/AlleriaLabs/Allerion_Server/main/install.sh | sudo bash

Optional custom directory:

    APP_DIR=/srv/allerion sudo -E bash install.sh

The installer detects x86_64/ARM64, creates private state directories, generates a random one-time setup credential, builds the service and checks health. It preserves an existing .env, data/ and secrets/. If the target directory contains unrelated files, it stops rather than overwriting them.

Open the Android app, add a server using http://SERVER-IP:8081, and create the first administrator using the one-time setup token printed by the installer. Choose a unique password of at least 12 characters. The token is used only for first-admin setup on new installations.

## Security

- Do not expose port 8081 directly to the public internet.
- Password sign-in over plain HTTP is suitable only on a trusted private LAN. Use a VPN or configure HTTPS through a reverse proxy before remote access.
- New installations set LEGACY_TOKEN_AUTH=false: the old token is accepted for first-admin setup but not as a permanent administrator login.
- Existing upgrades preserve compatibility by keeping the current LEGACY_TOKEN_AUTH value (or the backwards-compatible default). After every device has migrated to password sessions, set LEGACY_TOKEN_AUTH=false in .env and recreate the service.
- Docker inspection uses a restricted socket proxy with write operations disabled. Do not publish its port.

## Update and backup

From the installation directory:

    ./backup.sh
    ./update.sh

Backups include .env, data/, secrets/ and the Compose file. Keep backup archives private. The updater creates a backup before fast-forwarding the main branch and rebuilding. It never runs docker compose down -v and never removes persistent data.

For manual restore, stop the stack, copy the current installation directory elsewhere, then restore the archive contents into the installation directory and start the stack again.

## Troubleshooting

    docker compose ps
    docker compose logs --tail=150 allerion-server
    curl -fsS http://127.0.0.1:8081/health

Remove tokens, passwords, API keys, device credentials and public IP details before sharing logs.
