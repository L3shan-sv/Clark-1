# order-service/Dockerfile
# Multi-stage build — builder installs deps, runtime is lean

FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Security: non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser

WORKDIR /app

# Copy only installed packages from builder
COPY --from=builder /install /usr/local

# Copy shared observability module
COPY ../shared/ ./shared/

# Copy service code
COPY main.py .

# Writable temp dir for the non-root user
RUN mkdir -p /tmp/order-service && chown appuser:appuser /tmp/order-service

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live')"

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    # Order service specific defaults
    SERVICE_NAME=order-service \
    PORT=8000 \
    WORKERS=2 \
    OTLP_ENDPOINT=http://otel-collector:4317

EXPOSE 8000

# Uvicorn with graceful shutdown support
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT --workers $WORKERS --timeout-graceful-shutdown 30"]
