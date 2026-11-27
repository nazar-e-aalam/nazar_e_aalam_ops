#!/usr/bin/env bash
# Local run on http://localhost:8100/ops/  (panel)  and  http://localhost:8100/applications  (public)
set -euo pipefail
cd "$(dirname "$0")/.."
export OPS_ROOT_DOMAIN="${OPS_ROOT_DOMAIN:-localhost}" OPS_DEV=true OPS_HTTPS=false
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload
