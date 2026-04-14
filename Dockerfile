FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libgl1 \
        libglib2.0-0 \
        unzip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install gdown

COPY app ./app
COPY for_admin ./for_admin
COPY setup_scripts ./setup_scripts
COPY .env.example ./.env.example

EXPOSE 8000

CMD ["bash", "-lc", "if [ ! -f \"${YOLO_WEIGHTS_PATH:-/app/weights/yolo/yolo26x.pt}\" ]; then echo \"YOLO weights not found at ${YOLO_WEIGHTS_PATH:-/app/weights/yolo/yolo26x.pt}. Trying download_weights.sh...\"; bash /app/setup_scripts/download_weights.sh; fi && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
