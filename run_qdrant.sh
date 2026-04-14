#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

QDRANT_BIN="${QDRANT_BIN:-qdrant}"
if ! command -v "${QDRANT_BIN}" >/dev/null 2>&1; then
  echo "qdrant binary not found in PATH."
  echo "Set QDRANT_BIN to the binary path, or install Qdrant in this container."
  exit 1
fi

STORAGE_DIR="${QDRANT_STORAGE_DIR:-${ROOT_DIR}/qdrant_storage}"
HTTP_PORT="${QDRANT_HTTP_PORT:-6335}"
GRPC_PORT="${QDRANT_GRPC_PORT:-6336}"

mkdir -p "${STORAGE_DIR}"

export QDRANT__SERVICE__HOST="0.0.0.0"
export QDRANT__SERVICE__HTTP_PORT="${HTTP_PORT}"
export QDRANT__SERVICE__GRPC_PORT="${GRPC_PORT}"
export QDRANT__STORAGE__STORAGE_PATH="${STORAGE_DIR}"

exec "${QDRANT_BIN}"
