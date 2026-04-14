#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${SCRIPT_DIR}/.venv}"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/.env}"

if [[ ! -f "${VENV_DIR}/bin/activate" ]]; then
  echo "venv activate script not found: ${VENV_DIR}/bin/activate"
  echo "Run ./setup_env.sh first, or set VENV_DIR."
  exit 1
fi
source "${VENV_DIR}/bin/activate"

set -a
if [[ -f "${ENV_FILE}" ]]; then
  source "${ENV_FILE}"
else
  echo "env file not found: ${ENV_FILE} (continue without it)"
fi
set +a

# Default process timezone (override by pre-setting TZ before running this script).
: "${TZ:=Asia/Seoul}"
export TZ

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8009}"
exec uvicorn app.main:app --host "${HOST}" --port "${PORT}"
