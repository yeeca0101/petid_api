#!/bin/bash
set -e

GOOGLE_DRIVE_URL="${GOOGLE_DRIVE_URL:-https://drive.google.com/file/d/YOUR_FILE_ID/view?usp=sharing}"
ZIP_FILE="weights.zip"
WEIGHTS_DIR="../weights"

echo "=== Weights 다운로드 시작 (URL: $GOOGLE_DRIVE_URL) ==="

# gdown 설치 확인
if ! command -v gdown &> /dev/null; then
    echo "gdown 설치 중..."
    apt-get update -qq
    apt-get install -y python3-pip
    pip3 install --break-system-packages gdown
    echo "gdown 설치 완료!"
else
    echo "gdown 이미 설치됨 (버전: $(pip3 show gdown | grep Version))"
fi

mkdir -p "$WEIGHTS_DIR"
cd "$WEIGHTS_DIR"

echo "다운로드 중..."
gdown "$GOOGLE_DRIVE_URL" -O "$ZIP_FILE"

if [ ! -s "$ZIP_FILE" ] || [ $(stat -c %s "$ZIP_FILE") -lt 10000 ]; then
    echo "❌ 다운로드 실패: 파일 크기가 너무 작습니다 (HTML 에러 페이지일 가능성 높음)"
    echo "파일 정보:"
    ls -l "$ZIP_FILE"
    exit 1
fi

echo "압축 해제 중..."
unzip -o "$ZIP_FILE" && rm -f "$ZIP_FILE"

echo "완료! ls -la 결과:"
ls -la

echo "=== 다운로드 완료! ==="