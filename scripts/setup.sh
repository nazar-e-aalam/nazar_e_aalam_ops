#!/usr/bin/env bash
# One-time setup: virtualenv + dependencies + .env
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || { echo "Python 3.10+ is required"; exit 1; }
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo "Created .env, edit it now (domain, admin email)."; }
mkdir -p data
echo "Done. Start locally with: scripts/dev.sh"
