# Allerion Server — public self-hosted deployment

Allerion runs on infrastructure you control. Monitoring data, accounts and integration credentials stay on your server. No Alleria cloud account or subscription is required.

## Supported platforms

- Linux x86_64 and ARM64 (aarch64)
- Docker Engine and Docker Compose v2
- CasaOS, Debian 12/13, Ubuntu Server and Raspberry Pi OS 64-bit
- LAN access; remote access requires HTTPS through a reverse proxy or a VPN

Windows/macOS and non-Docker deployments are not supported by this installer.

## One-command install

Install Docker Engine and Docker Compose v2 first. The installer also requires Git, OpenSSL and Python 3 (normally available on supported Linux distributions). Then run this single command as an administrator:

    curl -fsSL https://raw.githubusercontent.com/AlleriaLabs/Allerion_Server/main/install.sh | sudo bash

The installer automatically:
- detects supported CPU architecture;
- downloads the published multi-architecture container image from GHCR (or builds from the public source checkout if the image is unavailable);
- creates private `.env`, `data/`, `secrets/` and `backups/` directories;
- generates a random first-admin setup token;
- starts the service and checks its health.

You do not create or upload these files or folders yourself. They are internal application state managed by the installer. On an existing installation, the installer preserves the current `.env`, database, secrets and monitoring history.

Optional custom directory:

    curl -fsSL https://raw.githubusercontent.com/AlleriaLabs/Allerion_Server/main/install.sh | sudo env APP_DIR=/srv/allerion bash -s

Open the Android app, add a server using `http://SERVER-IP:8081`, and create the first administrator using the one-time setup token printed by the installer. Choose a unique password of at least 12 characters.

## Optional integrations

After installation, run:

    cd /DATA/AppData/Allerion_Server
    sudo bash configure.sh

The guided setup can configure timezone, Jellyfin URL/API key, Telegram bot/chat, and alert webhook details. Secret inputs are hidden while typing. All values are stored locally in the private `.env` file. Leave a prompt blank to keep its current value.

Other advanced settings can be edited in `.env`; restart with `docker compose up -d`.

## Update

From the installation directory:

    sudo bash update.sh

The updater creates a private backup, fast-forwards the Git checkout, pulls the published image for the host architecture (falling back to a local source build if the image cannot be pulled), and recreates the service. It does not delete persistent data or volumes.

## Backup and restore

Create a backup:

    sudo bash backup.sh

Backups include `.env`, `data/`, `secrets/` and the Compose file. Keep backup archives private. To restore, stop the stack, extract the selected archive into the installation directory, then start with `docker compose up -d`.

## Uninstall

Stop and remove containers while preserving all configuration and history:

    sudo bash uninstall.sh

To permanently delete the entire installation and all local Allerion data, use:

    sudo bash uninstall.sh --purge

The purge requires typing an explicit confirmation phrase. Export a backup first if you may need the data later.

## Security

- Do not expose the Allerion port (8081) or Docker socket proxy (2375) directly to the public internet.
- Password sign-in over plain HTTP is suitable only on a trusted private LAN. Use a VPN or configure HTTPS through a reverse proxy before remote access.
- New installations set `LEGACY_TOKEN_AUTH=false`; the setup token is for first-admin setup, not ongoing login.
- Existing upgrades preserve the current legacy-auth setting. After every device has migrated to password sessions, set `LEGACY_TOKEN_AUTH=false` and recreate the service.
- Docker inspection uses a restricted socket proxy with write operations disabled.

## Troubleshooting

    docker compose ps
    docker compose logs --tail=150 allerion-server
    curl -fsS http://127.0.0.1:8081/health

Remove tokens, passwords, API keys, device credentials and public IP details before sharing logs.
