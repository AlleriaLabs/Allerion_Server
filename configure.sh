#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
[[ -f .env ]] || { echo "No .env found. Run install.sh first." >&2; exit 1; }
command -v python3 >/dev/null || { echo "Python 3 is required." >&2; exit 1; }
chmod 600 .env

set_env() {
  local key="$1" value="$2"
  printf '%s' "$value" | python3 -c '
import os, pathlib, sys
key = sys.argv[1] + "="
value = sys.stdin.read()
path = pathlib.Path(".env")
lines = path.read_text().splitlines()
out = []
updated = False
for line in lines:
    if line.startswith(key):
        if not updated:
            out.append(key + "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'")
            updated = True
    else:
        out.append(line)
if not updated:
    out.append(key + value)
path.write_text("\n".join(out) + "\n")
os.chmod(path, 0o600)
' "$key"
}

echo "Allerion optional integration setup"
echo "Leave a value blank to keep its current setting."
read -r -p "Timezone [Asia/Manila]: " tz
[[ -n "$tz" ]] && set_env TZ "$tz"
read -r -p "Jellyfin server URL (e.g. http://192.168.1.10:8096): " jf_url
[[ -n "$jf_url" ]] && set_env JELLYFIN_URL "$jf_url"
read -r -s -p "Jellyfin API key (input hidden): " jf_key; echo
[[ -n "$jf_key" ]] && set_env JELLYFIN_API_KEY "$jf_key"
read -r -s -p "Telegram bot token (input hidden): " tg_token; echo
[[ -n "$tg_token" ]] && set_env TELEGRAM_BOT_TOKEN "$tg_token"
read -r -p "Telegram chat ID: " tg_chat
[[ -n "$tg_chat" ]] && set_env TELEGRAM_CHAT_ID "$tg_chat"
read -r -p "Alert webhook URL: " webhook
[[ -n "$webhook" ]] && set_env ALERT_WEBHOOK_URL "$webhook"
read -r -s -p "Alert webhook secret (input hidden): " webhook_secret; echo
[[ -n "$webhook_secret" ]] && set_env ALERT_WEBHOOK_SECRET "$webhook_secret"

echo "Settings saved with restrictive permissions. Restarting Allerion to apply them..."
docker compose up -d
echo "Done. Check status with: docker compose ps"
