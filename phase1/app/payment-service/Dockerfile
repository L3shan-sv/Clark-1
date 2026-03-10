# payment-service/Dockerfile
# Payment service gets the strictest security config.
# Longer graceful shutdown — transactions must complete before pod dies.

FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim AS runtime

RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser

WORKDIR /app

COPY --from=builder /install /usr/local
COPY ../shared/ ./shared/
COPY main.py .

RUN mkdir -p /tmp/payment-service && chown appuser:appuser /tmp/payment-service

# Payment service health check is slightly more generous on startup
# (it initialises Kafka producer on startup which takes a moment)
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live')"

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    SERVICE_NAME=payment-service \
    PORT=8000 \
    # Single worker — payment service is stateful (Kafka producer)
    # Multiple workers would create multiple producers pointing at same group
    WORKERS=1 \
    OTLP_ENDPOINT=http://otel-collector:4317 \
    FRAUD_AMOUNT_THRESHOLD=10000

EXPOSE 8000

# 60s graceful shutdown — payments in flight must complete
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT --workers $WORKERS --timeout-graceful-shutdown 60"]
