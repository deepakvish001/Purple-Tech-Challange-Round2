# Two-stage build.
#
# The default stage ("synthetic") is lean — no torch, no opencv. It runs
# `python -m services.ingest` which by default executes the synthetic
# publisher.
#
# The `video` stage adds CPU-only torch + ultralytics + opencv + supervision
# and is what `docker compose --profile video up` picks up for the
# per-camera workers. We pin to CPU wheels so we don't pull CUDA on a
# laptop.

# ---------- base (synthetic) ----------
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --no-cache-dir \
    "pydantic>=2.7" "pyyaml>=6.0" "redis[hiredis]>=5.0" \
    "structlog>=24.1" "python-dateutil>=2.9"

COPY services /app/services

CMD ["python", "-m", "services.ingest"]


# ---------- video ----------
FROM base AS video

# OpenCV runtime deps for headless decoding.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

# CPU-only torch + the detection stack.
RUN pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    "torch==2.4.*" "torchvision==0.19.*" \
 && pip install --no-cache-dir \
    "ultralytics>=8.2" "supervision>=0.21" "opencv-python-headless>=4.10"

# Bake the YOLOv8n weights into the image so the first frame doesn't pay
# the cold-start network hit. Falls back gracefully if the download fails
# at build time (the runtime will retry).
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')" || true
