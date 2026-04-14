#!/usr/bin/env bash
set -euo pipefail

API_BASE="${API_BASE:-http://localhost:8001}"

curl -sS "${API_BASE}/v1/health" | cat
