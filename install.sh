#!/usr/bin/env bash
set -Eeuo pipefail

REPO="https://github.com/AlleriaLabs/Allerion_Server.git"
IMAGE="ghcr.io/allerialabs/allerion-server:latest"
APP_DIR="${APP_DIR:-}"
if [[ -z "$APP_DIR" ]]; then
  if [[ -d /DATA/AppData ]]; then APP_DIR="/DATA/AppData/Allerion_Server"; else APP_DIR="/opt/allerion"; fi
fi
fail(){ echo "[Allerion] ERROR: $*" >&2; exit 1; }
info(){ echo "[Allerion] $*"; }

[[ "$(uname -s)" == "Linux" ]] || fail "Linux hosts only."
case "$(uname -m)" in x86_64|aarch64|arm64) ;; *) fail "Supported architectures: x86_64 and ARM64." ;; esac
command -v docker >/dev/null || fail "Docker Engine is required. Install Docker first."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required."
command -v git >/dev/null || fail "Git is required."
command -v openssl >/dev/null || fail "OpenSSL is required."
command -v python3 >/dev/null || fail "Python 3 is required for the local health check."

info "Preparing $APP_DIR"
mkdir -p "$(dirname "$APP_DIR")"
if [[ -d "$APP_DIR/.git" ]]; then
  info "Existing Git checkout detected; preserving configuration and data."
elif [[ -d "$APP_DIR" ]] && [[ -n "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  fail "$APP_DIR is non-empty and is not an Allerion Git checkout. Nothing was overwritten."
else
  git clone --branch main --depth 1 "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"
chmod 700 install.sh backup.sh update.sh uninstall.sh configure.sh 2>/dev/null || true
mkdir -p data secrets backups
chmod 700 data secrets backups

CREATED_ENV=0
if [[ ! -f .env ]]; then
  umask 077
  cat > .env <<EOF
TOKEN=$(openssl rand -hex 32)
LEGACY_TOKEN_AUTH=false
ALLERION_IMAGE=$IMAGE
PORT=8081
TZ=Asia/Manila
ENABLE_LAN_DISCOVERY=1
SERVICE_CHECKS=
JELLYFIN_URL=
JELLYFIN_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ALERT_WEBHOOK_URL=
ALERT_WEBHOOK_SECRET=
ALERT_CPU_THRESHOLD=90
ALERT_RAM_THRESHOLD=90
ALERT_TEMP_THRESHOLD=85
ALERT_DISK_THRESHOLD=92
PRICE_PER_KWH=0
CURRENCY=PHP
BASE_WATTS=0
EOF
  chmod 600 .env
  CREATED_ENV=1
  info "Generated private configuration and one-time setup credential."
else
  chmod 600 .env
  info "Existing .env preserved."
fi

# Use the published multi-architecture image; no source build or manual Docker setup is needed.
docker compose pull
docker compose up -d --remove-orphans

PORT_VALUE="$(sed -n 's/^PORT=//p' .env | tail -n 1)"
PORT_VALUE="${PORT_VALUE:-8081}"
HEALTH_URL="http://127.0.0.1:${PORT_VALUE}/health"
for attempt in $(seq 1 45); do
  if python3 -c 'import sys,urllib.request; urllib.request.urlopen(sys.argv[1],timeout=2)' "$HEALTH_URL" >/dev/null 2>&1; then break; fi
  sleep 2
done
python3 -c 'import sys,urllib.request; urllib.request.urlopen(sys.argv[1],timeout=3)' "$HEALTH_URL" || fail "Health check failed. Run: docker compose logs --tail=150 allerion-server"

info "Installation complete."
echo "Server URL: http://<server-ip>:$PORT_VALUE"
if [[ "$CREATED_ENV" == "1" ]]; then
  echo -n "One-time administrator setup token: "
  sed -n 's/^TOKEN=//p' .env | head -n 1
  echo
  echo "Use this token only for first-admin setup. Store it securely."
else
  echo "Existing credentials and monitoring data were preserved."
fi
echo "Optional integrations: run sudo bash configure.sh"
echo "Directory: $APP_DIR"
