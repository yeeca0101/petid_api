#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

API_BASE="${API_BASE:-http://localhost:8001}"
FORMAT="${FORMAT:-json}"
IMG1="${IMG1:-}"
IMG2="${IMG2:-}"
DEFAULT_IMG1="${DEFAULT_IMG1:-}"
DEFAULT_IMG2="${DEFAULT_IMG2:-}"

if [ -z "${DEFAULT_IMG1}" ] || [ -z "${DEFAULT_IMG2}" ]; then
  mapfile -t _default_imgs < <(find "${PROJECT_ROOT}/data/images_for_test" -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) 2>/dev/null | sort | head -n 2)
  if [ -z "${DEFAULT_IMG1}" ] && [ "${#_default_imgs[@]}" -ge 1 ]; then
    DEFAULT_IMG1="${_default_imgs[0]}"
  fi
  if [ -z "${DEFAULT_IMG2}" ] && [ "${#_default_imgs[@]}" -ge 2 ]; then
    DEFAULT_IMG2="${_default_imgs[1]}"
  fi
fi

if [ -z "${IMG1}" ] && [ -f "${DEFAULT_IMG1}" ]; then
  IMG1="${DEFAULT_IMG1}"
fi
if [ -z "${IMG2}" ] && [ -f "${DEFAULT_IMG2}" ]; then
  IMG2="${DEFAULT_IMG2}"
fi

if [ -z "${IMG1}" ] || [ -z "${IMG2}" ] || [ ! -f "${IMG1}" ] || [ ! -f "${IMG2}" ]; then
  echo "Usage: IMG1=/path/a.jpg IMG2=/path/b.jpg $0" >&2
  echo "Hint: set IMG1/IMG2, or place files at ${DEFAULT_IMG1} and ${DEFAULT_IMG2}" >&2
  exit 1
fi

curl -sS -X POST "${API_BASE}/v1/embed/batch?format=${FORMAT}" \
  -F "files=@${IMG1}" \
  -F "files=@${IMG2}" | cat
