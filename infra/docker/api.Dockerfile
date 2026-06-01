FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --no-cache-dir \
    "pydantic>=2.7" "redis[hiredis]>=5.0" \
    "fastapi>=0.115" "uvicorn[standard]>=0.30" \
    "structlog>=24.1" "prometheus-client>=0.20"

COPY services /app/services

EXPOSE 8000
CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
