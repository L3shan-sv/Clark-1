# notification-service/Dockerfile
# Notification service is event-driven (Kafka consumer).
# Single worker — the consumer loop runs as a background asyncio task,
# multiple workers would create duplicate consumers in the same group.

FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim AS runtime

RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser

WORKDIR /app

COPY --from=builder /install /usr/local
COPY ../shared/ ./shared/
COPY main.py .

RUN mkdir -p /tmp/notification-service && chown appuser:appuser /tmp/notification-service

# Longer start period — Kafka consumer needs to connect and join consumer group
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready')"

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    SERVICE_NAME=notification-service \
    PORT=8000 \
    WORKERS=1 \
    OTLP_ENDPOINT=http://otel-collector:4317

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT --workers $WORKERS --timeout-graceful-shutdown 30"]
