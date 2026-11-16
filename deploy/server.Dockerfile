FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/apps/server

WORKDIR /app/apps/server

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY apps/server/requirements.txt /app/apps/server/requirements.txt
RUN pip install --no-cache-dir -r /app/apps/server/requirements.txt

COPY apps/server/app /app/apps/server/app
COPY apps/server/scripts /app/apps/server/scripts
COPY releases/windows/LingzhuJianyingImporter.exe /app/releases/windows/LingzhuJianyingImporter.exe

RUN mkdir -p /app/apps/server/data/uploads /app/apps/server/data/assets /app/apps/server/data/previews /app/apps/server/data/exports /app/apps/server/data/logs \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app/apps/server

USER appuser
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
