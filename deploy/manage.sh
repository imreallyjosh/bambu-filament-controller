#!/bin/bash
# Run Flask CLI commands with the production environment.
# Usage: sudo -u bambufc /opt/bambu-filament-controller/deploy/manage.sh <command> [args]
#   e.g. create-user josh | db upgrade | seed | check-controller
set -euo pipefail
set -a
. /etc/bambu-filament-controller/controller.env
set +a
cd /opt/bambu-filament-controller
exec /opt/bambu-filament-controller/venv/bin/flask --app wsgi:app "$@"
