FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir \
    "pydantic>=2.7" "pyyaml>=6.0" "redis[hiredis]>=5.0" \
    "structlog>=24.1" "python-dateutil>=2.9"

COPY services /app/services

CMD ["python", "-m", "services.ingest"]
