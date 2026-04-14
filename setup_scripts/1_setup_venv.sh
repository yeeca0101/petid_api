#!/usr/bin/env bash
set -euo pipefail

# 스크립트가 있는 디렉토리 (setup_scripts)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 프로젝트 루트 디렉토리 (setup_scripts의 상위 폴더)
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-${PROJECT_ROOT}/.venv}"            # 상위 폴더에 .venv 생성
REQ_FILE="${REQ_FILE:-${PROJECT_ROOT}/requirements.txt}" # 프로젝트 루트의 requirements.txt 사용

echo "=== Virtual Environment Setup 시작 ==="
echo "프로젝트 루트: ${PROJECT_ROOT}"
echo "requirements.txt 경로: ${REQ_FILE}"
echo "가상환경 경로: ${VENV_DIR}"

install_python_if_missing() {
  local missing=()

  if ! command -v python3 >/dev/null 2>&1; then
    missing+=("python3")
  fi

  if ! command -v pip3 >/dev/null 2>&1; then
    missing+=("python3-pip")
  fi

  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "⚠️ 다음 패키지가 필요하지만 설치되어 있지 않습니다: ${missing[*]}"
    read -r -p "우분투에 Python/pip을 설치하시겠습니까? [y/n] " answer

    case "${answer,,}" in
      y|yes)
        echo "필수 패키지 설치 중..."
        if command -v sudo >/dev/null 2>&1; then
          sudo apt update
          sudo apt install -y python3 python3-pip python3-venv
        else
          apt update
          apt install -y python3 python3-pip python3-venv
        fi
        ;;
      *)
        echo "❌ Python/pip이 없어 설치를 진행할 수 없습니다."
        exit 1
        ;;
    esac
  fi
}

# Python/pip 확인 및 필요 시 설치
install_python_if_missing

# requirements.txt 존재 여부 확인
if [[ ! -f "${REQ_FILE}" ]]; then
  echo "❌ requirements file not found: ${REQ_FILE}"
  echo "   프로젝트 루트에 requirements.txt 파일이 있는지 확인해주세요."
  exit 1
fi

# virtualenv 설치/업데이트
echo "virtualenv 업그레이드 중..."
python3 -m pip install --upgrade virtualenv

# 가상환경 생성
echo "가상환경 생성 중... (${VENV_DIR})"
"${PYTHON_BIN}" -m virtualenv "${VENV_DIR}"

# pip 업그레이드 및 패키지 설치
echo "pip 업그레이드 및 패키지 설치 중..."
"${VENV_DIR}/bin/python" -m pip install --upgrade pip setuptools wheel
"${VENV_DIR}/bin/python" -m pip install -r "${REQ_FILE}"

echo "✅ venv 준비 완료: ${VENV_DIR}"
echo "   활성화 방법: source ${VENV_DIR}/bin/activate"