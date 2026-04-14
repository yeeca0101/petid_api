#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

GOOGLE_DRIVE_URL="${GOOGLE_DRIVE_URL:-}"
ZIP_FILE="${ZIP_FILE:-weights.zip}"
WEIGHTS_DIR="${WEIGHTS_DIR:-${PROJECT_ROOT}/weights}"
YOLO_WEIGHTS_PATH="${YOLO_WEIGHTS_PATH:-${WEIGHTS_DIR}/yolo/yolo26x.pt}"

if [[ -z "${GOOGLE_DRIVE_URL}" ]]; then
    echo "❌ GOOGLE_DRIVE_URL is not set."
    echo "   Provide it only at runtime, for example:"
    echo "   GOOGLE_DRIVE_URL='https://drive.google.com/...' bash setup_scripts/download_weights.sh"
    exit 1
fi

echo "=== Weights 다운로드 시작 ==="
echo "weights dir: ${WEIGHTS_DIR}"
echo "target yolo weights: ${YOLO_WEIGHTS_PATH}"

if ! command -v gdown >/dev/null 2>&1; then
    echo "❌ gdown not found. Install it first in this environment."
    exit 1
fi

if ! command -v unzip >/dev/null 2>&1; then
    echo "❌ unzip not found. Install it first in this environment."
    exit 1
fi

mkdir -p "${WEIGHTS_DIR}"
cd "${WEIGHTS_DIR}"

echo "다운로드 중..."
gdown "${GOOGLE_DRIVE_URL}" -O "${ZIP_FILE}"

if [[ ! -s "${ZIP_FILE}" ]] || [[ $(stat -c %s "${ZIP_FILE}") -lt 10000 ]]; then
    echo "❌ 다운로드 실패: 파일 크기가 너무 작습니다 (HTML 에러 페이지일 가능성 높음)"
    echo "파일 정보:"
    ls -l "${ZIP_FILE}"
    exit 1
fi

echo "압축 해제 중..."
unzip -o "${ZIP_FILE}"
rm -f "${ZIP_FILE}"

if [[ ! -f "${YOLO_WEIGHTS_PATH}" ]]; then
    echo "❌ 다운로드/압축 해제 후에도 YOLO weight 파일이 없습니다: ${YOLO_WEIGHTS_PATH}"
    exit 1
fi

echo "완료! weights 디렉터리:"
ls -la
echo "=== 다운로드 완료! ==="
