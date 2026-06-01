FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --no-cache-dir \
    "pydantic>=2.7" "redis[hiredis]>=5.0" "structlog>=24.1"

COPY services /app/services

CMD ["python", "-m", "services.pos"]
