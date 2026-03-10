# Multi-stage build — lean production image
# Usage: docker build --build-arg SERVICE=order-service -t order-service .

FROM python:3.12-slim AS base

# Security: run as non-root
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Install deps first (layer caching)
COPY shared/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared observability module
COPY shared/ ./shared/

# Build arg selects which service to copy
ARG SERVICE
COPY ${SERVICE}/ ./${SERVICE}/

# Health check
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health/live').raise_for_status()"

USER appuser

# OTEL agent configuration via env vars (set in k8s manifests)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OTLP_ENDPOINT=http://otel-collector:4317

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
