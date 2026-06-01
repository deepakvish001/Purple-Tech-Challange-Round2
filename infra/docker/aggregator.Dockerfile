FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "pydantic>=2.7" "redis[hiredis]>=5.0" \
    "psycopg[binary,pool]>=3.2" "structlog>=24.1"

COPY services /app/services

CMD ["python", "-m", "services.aggregator"]
