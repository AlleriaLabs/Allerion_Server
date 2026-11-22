#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_DIR:-$APP_DIR/backups}"
mkdir -p "$DEST"
chmod 700 "$DEST"
ARCHIVE="$DEST/allerion-backup-$STAMP.tar.gz"
ITEMS=()
for item in .env data secrets docker-compose.yml; do [[ -e "$item" ]] && ITEMS+=("$item"); done
if [[ "${#ITEMS[@]}" -eq 0 ]]; then echo "No Allerion state files found." >&2; exit 1; fi
tar -czf "$ARCHIVE" "${ITEMS[@]}"
chmod 600 "$ARCHIVE"
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"
chmod 600 "$ARCHIVE.sha256"
echo "Backup created: $ARCHIVE"
