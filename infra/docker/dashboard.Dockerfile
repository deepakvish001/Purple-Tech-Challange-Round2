FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --no-cache-dir \
    "streamlit>=1.36" \
    "pandas>=2.2" \
    "httpx>=0.27"

COPY services /app/services

EXPOSE 8501
CMD ["streamlit", "run", "services/dashboard/app.py", \
     "--server.port=8501", "--server.address=0.0.0.0", \
     "--browser.gatherUsageStats=false"]
