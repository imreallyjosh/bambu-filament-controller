#!/bin/bash
# Install or upgrade the Bambu Filament Controller.
#
#   sudo ./deploy/install.sh            # from the source checkout
#
# What it touches (nothing else):
#   user/group  bambufc (system account, no login shell)
#   /opt/bambu-filament-controller         code + venv   (root:bambufc 0750, read-only to service)
#   /var/lib/bambu-filament-controller     SQLite DB     (bambufc:bambufc 0750)
#   /etc/bambu-filament-controller         secrets env   (root:bambufc 0750 / file 0640)
#   /etc/systemd/system/bambu-fc-*         its own units only
# It never modifies other services. It does NOT enable MQTT or heater control.
set -euo pipefail

APP=/opt/bambu-filament-controller
DATA=/var/lib/bambu-filament-controller
ETC=/etc/bambu-filament-controller
ENVF=$ETC/controller.env
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USR=bambufc

[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
[[ -f "$SRC/wsgi.py" ]] || { echo "Cannot find source tree at $SRC" >&2; exit 1; }

echo "==> Checking port 8092 is free (or ours)"
if ss -ltnH 'sport = :8092' | grep -q . && ! systemctl is-active --quiet bambu-fc-web; then
  echo "Port 8092 is in use by another program. Edit deploy/gunicorn.conf.py and re-run." >&2; exit 1
fi

echo "==> Service account"
if ! getent passwd "$USR" >/dev/null; then
  useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin "$USR"
fi

echo "==> Directories"
install -d -o root -g "$USR" -m 0750 "$APP"
install -d -o "$USR" -g "$USR" -m 0750 "$DATA"
install -d -o "$USR" -g "$USR" -m 0750 "$DATA/camera"
install -d -o root -g "$USR" -m 0750 "$ETC"

echo "==> Copying application code"
rsync -a --delete --exclude venv --exclude instance --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '*.pyc' "$SRC/" "$APP/"
chown -R root:"$USR" "$APP"
chmod -R u=rwX,g=rX,o= "$APP"
chmod 0750 "$APP/deploy/manage.sh" "$APP/deploy/install.sh"

echo "==> Python virtual environment"
[[ -x "$APP/venv/bin/python" ]] || /usr/bin/python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --upgrade pip
"$APP/venv/bin/pip" install --quiet -r "$APP/requirements.txt"
chown -R root:"$USR" "$APP/venv"
chmod -R u=rwX,g=rX,o= "$APP/venv"

echo "==> Secrets file"
if [[ ! -f "$ENVF" ]]; then
  SECRET=$("$APP/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')
  FERNET=$("$APP/venv/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
  umask 027
  cat >"$ENVF" <<EOF
# Bambu Filament Controller secrets & switches. Mode 0640 root:$USR.
# Back up BFC_FERNET_KEY: without it the stored printer access codes can't be decrypted.
BFC_SECRET_KEY=$SECRET
BFC_FERNET_KEY=$FERNET
BFC_DATABASE_URI=sqlite:///$DATA/controller.db
# BFC_TIMEZONE=America/Chicago   # default: server timezone
# BFC_COOKIE_SECURE=true         # only if served over HTTPS
# BFC_PROXY_COUNT=1              # behind one reverse proxy (e.g. Nginx Proxy Manager)

# Safety switches - leave false until the corresponding phase is verified.
BFC_ENABLE_MQTT=false
BFC_ALLOW_HEATER_CONTROL=false
EOF
  echo "    created $ENVF"
else
  echo "    kept existing $ENVF"
fi
chown root:"$USR" "$ENVF"
chmod 0640 "$ENVF"

echo "==> Database migrations + templates"
runuser -u "$USR" -- "$APP/deploy/manage.sh" db upgrade
runuser -u "$USR" -- "$APP/deploy/manage.sh" seed

echo "==> systemd units"
for unit in bambu-fc-web.service bambu-fc-controller.service bambu-fc-camera.service bambu-fc-alert@.service \
            bambu-fc-watchdog.service bambu-fc-watchdog.timer; do
  install -o root -g root -m 0644 "$APP/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now bambu-fc-controller.service bambu-fc-web.service bambu-fc-camera.service bambu-fc-watchdog.timer
systemctl restart bambu-fc-controller.service bambu-fc-web.service bambu-fc-camera.service

if ! runuser -u "$USR" -- "$APP/venv/bin/python" - <<'PY'
import os, sqlite3, sys
db = os.environ.get("DB", "/var/lib/bambu-filament-controller/controller.db")
sys.exit(0 if sqlite3.connect(db).execute("select count(*) from user").fetchone()[0] else 1)
PY
then
  echo "==> No login user yet. Create one now:"
  read -rp "    Username: " NEWUSER
  runuser -u "$USR" -- "$APP/deploy/manage.sh" create-user "$NEWUSER"
fi

echo
echo "Done. Open http://$(hostname -I | awk '{print $1}'):8092/"
systemctl --no-pager --lines=0 status bambu-fc-controller.service bambu-fc-web.service bambu-fc-camera.service || true
