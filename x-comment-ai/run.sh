#!/usr/bin/env bash
set -euo pipefail
if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
exec uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
