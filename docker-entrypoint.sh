#!/bin/sh
# One image, several roles (see docker-compose.yml):
#   web         apply database migrations + preset templates, then serve the UI (gunicorn, port 8092)
#   controller  printer connections, heater supervision, scheduler (single instance only)
#   camera      on-demand camera snapshots
#   watchdog    alert via ntfy if the controller's heartbeat goes stale
#   manage ...  run a Flask CLI command, e.g. `manage create-user alice`
set -e

if [ -z "$BFC_SECRET_KEY" ] || [ -z "$BFC_FERNET_KEY" ]; then
  echo "BFC_SECRET_KEY and BFC_FERNET_KEY must be set in .env (run ./install.sh to generate them)." >&2
  exit 1
fi

role="${1:-web}"
[ $# -gt 0 ] && shift

case "$role" in
  web)
    flask db upgrade
    flask seed
    exec gunicorn --config deploy/gunicorn.conf.py wsgi:app
    ;;
  controller)
    exec python controller/daemon.py
    ;;
  camera)
    exec python controller/camera.py
    ;;
  watchdog)
    while true; do
      flask check-controller || true
      sleep 60
    done
    ;;
  manage)
    exec flask "$@"
    ;;
  *)
    exec "$role" "$@"
    ;;
esac
