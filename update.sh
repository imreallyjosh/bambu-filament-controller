#!/usr/bin/env bash
# One-shot updater for an existing install: pulls the latest code and rebuilds/restarts the containers.
# Your data (the "data" volume) and .env are left alone; database migrations and new preset data are
# applied automatically when the web container starts.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "== Bambu Filament Controller updater =="

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "Docker (with the 'docker compose' plugin) is required -- run ./install.sh first." >&2
    exit 1
fi

if [[ ! -f .env ]]; then
    echo "No .env found here -- this doesn't look like an existing install. Run ./install.sh instead." >&2
    exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
    echo "This checkout has local changes, so 'git pull' could conflict:" >&2
    git status --short >&2
    echo >&2
    echo "Commit, stash, or discard them, then re-run ./update.sh." >&2
    exit 1
fi

echo
echo "Note: restarting stops any drying or annealing run in progress (it is never resumed automatically)."
read -r -p "Continue? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || exit 0

echo
echo "Pulling the latest version..."
git pull --ff-only

echo
echo "Rebuilding and restarting containers..."
docker compose up -d --build

echo
echo "Done. Check progress with 'docker compose logs -f web controller'."
