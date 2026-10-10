#!/usr/bin/env bash
# One-shot installer for Bambu Filament Controller with Docker. Safe to re-run: it never overwrites a value
# you've already set in .env.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "== Bambu Filament Controller installer =="

# --- 1. Docker -----------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is not installed."
    read -r -p "Install it now via get.docker.com? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        curl -fsSL https://get.docker.com | sh
        sudo usermod -aG docker "$USER"
        echo "Docker installed. Log out and back in (or run 'newgrp docker'), then re-run this script."
        exit 0
    else
        echo "Install Docker (https://docs.docker.com/engine/install/) and re-run this script." >&2
        exit 1
    fi
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "Docker is installed but the 'docker compose' plugin isn't available." >&2
    echo "See https://docs.docker.com/compose/install/" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required (used only to generate random secrets for .env)." >&2
    exit 1
fi

# --- 2. .env ---------------------------------------------------------------
if [[ ! -f .env ]]; then
    cp .env.example .env
    chmod 600 .env
    echo "Created .env from .env.example."
fi

# Sets KEY=VALUE in .env, but only if KEY is currently missing or blank.
set_if_blank() {
    local key="$1" value="$2"
    if grep -qE "^${key}=.+" .env; then
        return
    fi
    if grep -qE "^${key}=" .env; then
        sed -i "s|^${key}=.*|${key}=${value}|" .env
    else
        printf '%s=%s\n' "$key" "$value" >> .env
    fi
}

random_token() {
    python3 -c "import secrets; print(secrets.token_urlsafe(48))"
}

fernet_key() {
    python3 -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
}

host_timezone() {
    timedatectl show -p Timezone --value 2>/dev/null \
        || cat /etc/timezone 2>/dev/null \
        || readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||' \
        || echo UTC
}

set_if_blank BFC_SECRET_KEY "$(random_token)"
set_if_blank BFC_FERNET_KEY "$(fernet_key)"
tz="$(host_timezone)"
set_if_blank BFC_TIMEZONE "${tz:-UTC}"
set_if_blank TZ "${tz:-UTC}"

# --- 3. Build and start ----------------------------------------------------
echo
echo "Building and starting containers (the first build takes a few minutes)..."
docker compose up -d --build

echo
echo "Waiting for the web app to become healthy..."
for _ in $(seq 1 60); do
    if [[ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q web)" 2>/dev/null)" == "healthy" ]]; then
        break
    fi
    sleep 2
done

# --- 4. First login user ---------------------------------------------------
users="$(docker compose exec -T web /srv/app/docker-entrypoint.sh manage user-count 2>/dev/null | tail -1 || echo 0)"
if [[ "$users" == "0" && -t 0 ]]; then
    echo
    echo "No login user yet. Create one now (password: at least 10 characters)."
    read -r -p "Username: " username
    docker compose exec web /srv/app/docker-entrypoint.sh manage create-user "$username"
fi

port="$(grep -E '^BFC_PORT=' .env | cut -d= -f2-)"
ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Done. Open http://${ip:-localhost}:${port:-8092} and log in."
echo "Next: add your printers, then read the README's 'Before you heat anything' section."
