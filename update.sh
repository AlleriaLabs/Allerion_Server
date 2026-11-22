#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"
command -v docker >/dev/null || { echo "Docker is required." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required." >&2; exit 1; }
[[ -d .git ]] || { echo "This install is not a Git checkout." >&2; exit 1; }
BRANCH="$(git -c safe.directory="$APP_DIR" branch --show-current)"
[[ "$BRANCH" == "main" ]] || { echo "Refusing update from branch $BRANCH; use main." >&2; exit 1; }
bash ./backup.sh
# Normalize script modes changed by older installer revisions before checking out updates.
chmod 644 install.sh backup.sh update.sh configure.sh uninstall.sh 2>/dev/null || true
git -c safe.directory="$APP_DIR" fetch origin main
git -c safe.directory="$APP_DIR" merge --ff-only origin/main
# Pull before recreating so a failed download leaves the currently running container untouched.
if ! docker compose pull; then
  echo "Published image could not be pulled; building from the updated source checkout instead."
  docker compose build
fi
docker compose up -d --remove-orphans
echo "Update complete. Check docker compose ps and logs."
