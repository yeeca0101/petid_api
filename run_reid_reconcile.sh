#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${SCRIPT_DIR}/.venv}"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/.env}"
declare -A PRESERVED_ENV_VARS=()

if [[ -f "${VENV_DIR}/bin/activate" ]]; then
  source "${VENV_DIR}/bin/activate"
else
  echo "venv activate script not found: ${VENV_DIR}/bin/activate"
  echo "Continue with current Python environment."
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found in current environment."
    exit 1
  fi
fi

if [[ -f "${ENV_FILE}" ]]; then
  while IFS= read -r line; do
    if [[ "${line}" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)= ]]; then
      key="${BASH_REMATCH[1]}"
      if [[ -n "${!key+x}" ]]; then
        PRESERVED_ENV_VARS["${key}"]="${!key}"
      fi
    fi
  done < "${ENV_FILE}"

  set -a
  source "${ENV_FILE}"
  set +a

  for key in "${!PRESERVED_ENV_VARS[@]}"; do
    export "${key}=${PRESERVED_ENV_VARS[${key}]}"
  done
else
  echo "env file not found: ${ENV_FILE} (continue without it)"
fi

: "${TZ:=Asia/Seoul}"
export TZ

exec python3 -m app.tools.reid_reconcile "$@"

