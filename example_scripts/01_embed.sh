#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

API_BASE="${API_BASE:-http://localhost:8001}"
FORMAT="${FORMAT:-json}"
IMG="${IMG:-}"
DEFAULT_IMG="${DEFAULT_IMG:-}"

if [ -z "${DEFAULT_IMG}" ]; then
  DEFAULT_IMG="$(find "${PROJECT_ROOT}/data/images_for_test" -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) 2>/dev/null | sort | head -n 1 || true)"
fi

if [ -z "${IMG}" ] && [ -f "${DEFAULT_IMG}" ]; then
  IMG="${DEFAULT_IMG}"
fi

if [ -z "${IMG}" ] || [ ! -f "${IMG}" ]; then
  echo "Usage: IMG=/path/to.jpg $0" >&2
  echo "Hint: set IMG, or place a file at ${DEFAULT_IMG}" >&2
  exit 1
fi

curl -sS -X POST "${API_BASE}/v1/embed?format=${FORMAT}" \
  -F "file=@${IMG}" | cat
