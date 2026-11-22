#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-preserve}"
case "$MODE" in
  preserve)
    echo "This will stop and remove Allerion containers only."
    echo "Your .env, data/, secrets/, backups/, and application files will remain in $APP_DIR."
    read -r -p "Continue? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
    cd "$APP_DIR"
    docker compose down --remove-orphans
    echo "Containers removed. Persistent files preserved at $APP_DIR."
    ;;
  --purge)
    echo "WARNING: this permanently deletes Allerion configuration, accounts, secrets, backups and monitoring history."
    read -r -p "Type DELETE ALL ALLERION DATA to confirm: " answer
    [[ "$answer" == "DELETE ALL ALLERION DATA" ]] || { echo "Cancelled."; exit 0; }
    cd "$APP_DIR"
    docker compose down --remove-orphans
    cd /
    [[ "$APP_DIR" != "/" && "$APP_DIR" != "/DATA" && "$APP_DIR" != "/DATA/AppData" && "$APP_DIR" != "/opt" ]] || { echo "Refusing unsafe purge path: $APP_DIR" >&2; exit 1; }
    rm -rf -- "$APP_DIR"
    echo "Allerion application and local data removed."
    ;;
  *)
    echo "Usage: bash uninstall.sh [preserve|--purge]" >&2
    exit 2
    ;;
esac
