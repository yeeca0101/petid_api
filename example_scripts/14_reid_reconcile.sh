#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

FORMAT="${FORMAT:-pretty}"
OUTPUT="${OUTPUT:-}"
FAIL_ON_CUTOVER_BLOCKERS="${FAIL_ON_CUTOVER_BLOCKERS:-0}"

args=( "--format" "${FORMAT}" )

if [[ -n "${OUTPUT}" ]]; then
  args+=( "--output" "${OUTPUT}" )
fi

if [[ "${FAIL_ON_CUTOVER_BLOCKERS}" == "1" ]]; then
  args+=( "--fail-on-cutover-blockers" )
fi

exec "${PROJECT_ROOT}/run_reid_reconcile.sh" "${args[@]}"

