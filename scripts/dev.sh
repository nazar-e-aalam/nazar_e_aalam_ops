#!/usr/bin/env bash
# Run locally with auto-reload.  First run (or after a pull) it sets everything up by itself.
#   http://localhost:8100/ops/  (panel)    http://localhost:8100/applications  (public)
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/_lib.sh

HASH_NOW="$(md5sum requirements.txt 2>/dev/null | cut -d' ' -f1 || cksum requirements.txt)"
if [ ! -x .venv/bin/uvicorn ] || [ ! -f .env ] || [ "$(cat .venv/.req-hash 2>/dev/null || true)" != "$HASH_NOW" ]; then
  scripts/setup.sh
fi
# MySQL on this machine may be stopped (after a reboot): start it
HOST="$(env_get .env OPS_DB_HOST)"; HOST="${HOST:-127.0.0.1}"
case "$HOST" in 127.0.0.1|localhost) db_port_open 127.0.0.1 "$(env_get .env OPS_DB_PORT || true)" || { say "Starting MySQL"; mysql_start || warn "MySQL is not running; the panel will keep waiting for it"; };; esac

export OPS_ROOT_DOMAIN="${OPS_ROOT_DOMAIN:-localhost}" OPS_DEV=true OPS_HTTPS=false PYTHONUNBUFFERED=1
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8100}" --reload --reload-dir app --reload-dir web
