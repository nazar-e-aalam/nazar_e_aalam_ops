#!/usr/bin/env bash
# One-command setup for local use (Linux / macOS):  scripts/setup.sh
#   python venv + every dependency  ->  MariaDB/MySQL installed, started, database + user created  ->  .env written.
# Safe to re-run any time. Options:  --admin-email you@example.com   --no-db (use an external MySQL: fill OPS_DB_* in .env first)
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/_lib.sh

ADMIN_EMAIL=""; NO_DB=0
while [ $# -gt 0 ]; do
  case "$1" in
    --admin-email) ADMIN_EMAIL="${2:?--admin-email needs a value}"; shift 2;;
    --no-db) NO_DB=1; shift;;
    -h|--help) sed -n '2,5p' "$0"; exit 0;;
    *) die "unknown option: $1";;
  esac
done

say "Python"
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  warn "Python 3.10+ not found, installing"
  pkg_install python3 python3 python python3 python3 || die "install Python 3.10+ and re-run"
  PY="$(find_python)" || die "Python 3.10+ is required"
fi
ok "$($PY --version)"

say "Virtual environment and libraries"
if [ ! -x .venv/bin/python ]; then
  $PY -m venv .venv 2>/dev/null || { have apt-get && pkg_install python3-venv >/dev/null 2>&1; rm -rf .venv; $PY -m venv .venv; } || die "could not create .venv (install python3-venv)"
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt || die "installing libraries failed: check your internet connection and re-run"
md5sum requirements.txt 2>/dev/null | cut -d' ' -f1 > .venv/.req-hash || cksum requirements.txt > .venv/.req-hash
ok "libraries installed"

say "Configuration (.env)"
if [ ! -f .env ]; then cp .env.example .env; chmod 600 .env; ok "created .env"; else ok "kept existing .env"; fi
[ -z "$ADMIN_EMAIL" ] || env_set .env OPS_ADMIN_EMAIL "$ADMIN_EMAIL"

if [ "$NO_DB" = 1 ]; then ok "skipping database setup (--no-db)"; else setup_database .env || true; fi

echo
echo "${B}${G}Ready.${N} Start it with:  ${B}./run.sh${N}"
echo "  Panel   http://localhost:8100/ops/        Public  http://localhost:8100/applications"
echo "  The first-login password is printed in the terminal on first start (or set OPS_ADMIN_PASSWORD in .env)."
