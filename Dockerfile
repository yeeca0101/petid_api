FROM pytorch/pytorch:2.1.2-cuda12.1-cudnn8-runtime

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

COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY app ./app
COPY for_admin ./for_admin
COPY setup_scripts ./setup_scripts
COPY .env.example ./.env.example
COPY run_api.sh ./run_api.sh
COPY run_queue_worker.sh ./run_queue_worker.sh
COPY run_reid_reconcile.sh ./run_reid_reconcile.sh
COPY run_legacy_reid_import.sh ./run_legacy_reid_import.sh

RUN chmod +x /app/run_api.sh /app/run_queue_worker.sh /app/run_reid_reconcile.sh /app/run_legacy_reid_import.sh

EXPOSE 8000

CMD ["bash", "-lc", "if [ ! -f \"${YOLO_WEIGHTS_PATH:-/app/weights/yolo/yolo26x.pt}\" ]; then echo \"YOLO weights not found at ${YOLO_WEIGHTS_PATH:-/app/weights/yolo/yolo26x.pt}. Trying download_weights.sh...\"; bash /app/setup_scripts/download_weights.sh; fi && exec /app/run_api.sh"]
