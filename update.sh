#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"
command -v docker >/dev/null || { echo "Docker is required." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required." >&2; exit 1; }
[[ -d .git ]] || { echo "This install is not a Git checkout." >&2; exit 1; }
BRANCH="$(git branch --show-current)"
[[ "$BRANCH" == "main" ]] || { echo "Refusing update from branch $BRANCH; use main." >&2; exit 1; }
bash ./backup.sh
git fetch origin main
git merge --ff-only origin/main
docker compose up -d --build
echo "Update complete. Check docker compose ps and logs."
