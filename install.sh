#!/usr/bin/env bash
set -Eeuo pipefail
REPO="https://github.com/AlleriaLabs/Allerion_Server.git"
APP_DIR="${APP_DIR:-}"
if [[ -z "$APP_DIR" ]]; then
  if [[ -d /DATA/AppData ]]; then APP_DIR="/DATA/AppData/Allerion_Server"; else APP_DIR="/opt/allerion"; fi
fi
fail(){ echo "[Allerion] ERROR: $*" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || fail "Linux hosts only."
case "$(uname -m)" in x86_64|aarch64|arm64) ;; *) fail "Supported architectures: x86_64 and ARM64." ;; esac
command -v docker >/dev/null || fail "Docker Engine is required."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required."
command -v git >/dev/null || fail "Git is required."
command -v openssl >/dev/null || fail "OpenSSL is required."
echo "[Allerion] Preparing $APP_DIR"
mkdir -p "$(dirname "$APP_DIR")"
if [[ -d "$APP_DIR/.git" ]]; then
  echo "[Allerion] Existing Git checkout detected; preserving configuration and data."
elif [[ -d "$APP_DIR" ]] && [[ -n "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  fail "$APP_DIR is non-empty and is not an Allerion Git checkout. Nothing was overwritten."
else
  git clone --branch main --depth 1 "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"
chmod 700 install.sh backup.sh update.sh 2>/dev/null || true
mkdir -p data secrets backups
CREATED_ENV=0
chmod 700 secrets data
if [[ ! -f .env ]]; then
  umask 077
  printf 'TOKEN=%s\nLEGACY_TOKEN_AUTH=false\nPORT=8081\nTZ=Asia/Manila\nENABLE_LAN_DISCOVERY=1\nSERVICE_CHECKS=\n' "$(openssl rand -hex 32)" > .env
  chmod 600 .env
  CREATED_ENV=1
  echo "[Allerion] Created private .env and one-time setup token."
else
  chmod 600 .env
  echo "[Allerion] Existing .env preserved."
fi
docker compose up -d --build
for attempt in $(seq 1 45); do
  if python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8081/health",timeout=2)' >/dev/null 2>&1; then break; fi
  sleep 2
done
python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8081/health",timeout=3)' || fail "Health check failed. Run: docker compose logs --tail=150 allerion-server"
echo "[Allerion] Installation complete."
echo "Server URL: http://<server-ip>:8081"
if [[ "$CREATED_ENV" == "1" ]]; then
  echo -n "One-time administrator setup token: "
  sed -n 's/^TOKEN=//p' .env | head -n 1
  echo
  echo "Use the token only for first-admin setup. Keep it private."
else
  echo "Existing credentials were preserved. If first-admin setup is pending, retrieve the setup token from the private .env file."
fi
echo "Directory: $APP_DIR"
