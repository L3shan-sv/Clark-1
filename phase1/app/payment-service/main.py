# payment-service/main.py

import os
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

import redis.asyncio as redis
from aiokafka import AIOKafkaProducer
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from prometheus_client import Counter, Histogram, Gauge
import structlog

import sys
sys.path.append("/app/shared")
from observability import (
    setup_logging, setup_tracing, setup_metrics,
    instrument_app, metrics_endpoint,
    make_request_counter, make_request_latency, make_active_requests,
)

# ── Config ────────────────────────────────────────────────────────────────────

SERVICE_NAME  = "payment-service"
REDIS_URL     = os.getenv("REDIS_URL", "redis://redis:6379")
KAFKA_BROKERS = os.getenv("KAFKA_BROKERS", "kafka:9092")
OTLP_ENDPOINT = os.getenv("OTLP_ENDPOINT", "http://otel-collector:4317")

# Simulated fraud threshold — in real life, this calls a fraud detection service
FRAUD_AMOUNT_THRESHOLD = float(os.getenv("FRAUD_AMOUNT_THRESHOLD", "10000"))

log    = setup_logging(SERVICE_NAME)
tracer = setup_tracing(SERVICE_NAME, OTLP_ENDPOINT)
meter  = setup_metrics(SERVICE_NAME, OTLP_ENDPOINT)

# ── Business Metrics ──────────────────────────────────────────────────────────

request_counter = make_request_counter(SERVICE_NAME)
latency_hist    = make_request_latency(SERVICE_NAME)
active_requests = make_active_requests(SERVICE_NAME)

payments_processed = Counter(
    "payments_processed_total", "Total payments processed",
    ["status", "method"],
)
payments_revenue = Counter(
    "payments_revenue_dollars_total", "Total revenue processed in dollars",
)
payment_amount = Histogram(
    "payment_amount_dollars",
    "Distribution of payment amounts",
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000, 5000],
)
fraud_detections = Counter(
    "fraud_detections_total", "Payments flagged as potentially fraudulent",
    ["reason"],
)
refunds_issued = Counter(
    "refunds_issued_total", "Total refunds issued",
)
refund_amount = Counter(
    "refunds_amount_dollars_total", "Total dollars refunded",
)
payment_processing_time = Histogram(
    "payment_processing_duration_seconds",
    "Time to fully process a payment",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Payment Service", version="1.0.0")
app.add_route("/metrics", metrics_endpoint)
instrument_app(app, SERVICE_NAME)

# ── Models ────────────────────────────────────────────────────────────────────

class PaymentStatus(str, Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    REFUNDED = "refunded"
    FLAGGED  = "flagged"

class PaymentRequest(BaseModel):
    order_id: str
    amount:   float = Field(..., gt=0)
    user_id:  str
    method:   str = Field(default="card")

class PaymentResponse(BaseModel):
    payment_id: str
    order_id:   str
    amount:     float
    status:     PaymentStatus
    processed_at: str

class RefundRequest(BaseModel):
    reason: str = Field(default="customer_request")

# ── Dependencies ──────────────────────────────────────────────────────────────

async def get_redis():
    client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()

producer: Optional[AIOKafkaProducer] = None

@app.on_event("startup")
async def startup():
    global producer
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BROKERS)
    await producer.start()
    log.info("payment-service started")

@app.on_event("shutdown")
async def shutdown():
    if producer:
        await producer.stop()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health/live")
async def liveness():
    return {"status": "alive"}

@app.get("/health/ready")
async def readiness(r: redis.Redis = Depends(get_redis)):
    try:
        await r.ping()
        return {"status": "ready"}
    except Exception:
        raise HTTPException(status_code=503, detail="Redis not reachable")

@app.post("/payments", response_model=PaymentResponse, status_code=201)
async def process_payment(
    req: PaymentRequest,
    background_tasks: BackgroundTasks,
    r: redis.Redis = Depends(get_redis),
):
    import time
    start = time.perf_counter()
    payment_id = str(uuid.uuid4())

    log.info("processing payment",
        payment_id=payment_id,
        order_id=req.order_id,
        amount=req.amount,
        user_id=req.user_id,
    )

    with tracer.start_as_current_span("process_payment") as span:
        span.set_attribute("payment.id", payment_id)
        span.set_attribute("payment.order_id", req.order_id)
        span.set_attribute("payment.amount", req.amount)

        # Fraud detection
        if req.amount > FRAUD_AMOUNT_THRESHOLD:
            fraud_detections.labels(reason="amount_threshold").inc()
            payments_processed.labels(status="flagged", method=req.method).inc()
            await r.hset(f"payment:{payment_id}", mapping={
                "payment_id": payment_id,
                "order_id":   req.order_id,
                "amount":     req.amount,
                "status":     PaymentStatus.FLAGGED,
                "processed_at": datetime.utcnow().isoformat(),
            })
            log.warning("payment flagged for fraud review",
                payment_id=payment_id,
                amount=req.amount,
                threshold=FRAUD_AMOUNT_THRESHOLD,
            )
            span.set_attribute("payment.flagged", True)
            return PaymentResponse(
                payment_id=payment_id,
                order_id=req.order_id,
                amount=req.amount,
                status=PaymentStatus.FLAGGED,
                processed_at=datetime.utcnow().isoformat(),
            )

        # Process payment (in production: call Stripe/Adyen here)
        # Simulate occasional failures for realism
        import random
        if random.random() < 0.02:  # 2% failure rate
            payments_processed.labels(status="failed", method=req.method).inc()
            log.error("payment processing failed", payment_id=payment_id, reason="gateway_error")
            span.set_attribute("error", True)
            raise HTTPException(status_code=502, detail="Payment gateway error")

        # Success path
        processed_at = datetime.utcnow().isoformat()
        await r.hset(f"payment:{payment_id}", mapping={
            "payment_id":   payment_id,
            "order_id":     req.order_id,
            "amount":       str(req.amount),
            "status":       PaymentStatus.SUCCESS,
            "user_id":      req.user_id,
            "processed_at": processed_at,
        })
        await r.expire(f"payment:{payment_id}", 86400 * 30)  # 30 day retention

        # Record business metrics
        payments_processed.labels(status="success", method=req.method).inc()
        payments_revenue.inc(req.amount)
        payment_amount.observe(req.amount)
        payment_processing_time.observe(time.perf_counter() - start)

        # Publish payment event
        background_tasks.add_task(publish_payment_event, payment_id, req.order_id, req.amount)

        log.info("payment processed successfully",
            payment_id=payment_id,
            order_id=req.order_id,
            amount=req.amount,
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
        )

        return PaymentResponse(
            payment_id=payment_id,
            order_id=req.order_id,
            amount=req.amount,
            status=PaymentStatus.SUCCESS,
            processed_at=processed_at,
        )


@app.post("/payments/{payment_id}/refund", response_model=PaymentResponse)
async def refund_payment(
    payment_id: str,
    req: RefundRequest,
    background_tasks: BackgroundTasks,
    r: redis.Redis = Depends(get_redis),
):
    with tracer.start_as_current_span("refund_payment") as span:
        span.set_attribute("payment.id", payment_id)

        data = await r.hgetall(f"payment:{payment_id}")
        if not data:
            raise HTTPException(status_code=404, detail="Payment not found")

        if data.get("status") == PaymentStatus.REFUNDED:
            raise HTTPException(status_code=409, detail="Payment already refunded")

        amount = float(data["amount"])
        await r.hset(f"payment:{payment_id}", "status", PaymentStatus.REFUNDED)

        refunds_issued.inc()
        refund_amount.inc(amount)

        log.info("refund issued",
            payment_id=payment_id,
            amount=amount,
            reason=req.reason,
        )

        background_tasks.add_task(publish_refund_event, payment_id, amount)

        return PaymentResponse(
            payment_id=payment_id,
            order_id=data["order_id"],
            amount=amount,
            status=PaymentStatus.REFUNDED,
            processed_at=datetime.utcnow().isoformat(),
        )


@app.get("/payments/{payment_id}", response_model=PaymentResponse)
async def get_payment(payment_id: str, r: redis.Redis = Depends(get_redis)):
    data = await r.hgetall(f"payment:{payment_id}")
    if not data:
        raise HTTPException(status_code=404, detail="Payment not found")
    return PaymentResponse(**data)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def publish_payment_event(payment_id: str, order_id: str, amount: float):
    import json
    try:
        event = json.dumps({
            "event":      "payment.processed",
            "payment_id": payment_id,
            "order_id":   order_id,
            "amount":     amount,
            "ts":         datetime.utcnow().isoformat(),
        })
        await producer.send_and_wait("payments", event.encode())
    except Exception as e:
        log.error("failed to publish payment event", payment_id=payment_id, error=str(e))

async def publish_refund_event(payment_id: str, amount: float):
    import json
    try:
        event = json.dumps({
            "event":      "payment.refunded",
            "payment_id": payment_id,
            "amount":     amount,
            "ts":         datetime.utcnow().isoformat(),
        })
        await producer.send_and_wait("payments", event.encode())
    except Exception as e:
        log.error("failed to publish refund event", payment_id=payment_id, error=str(e))
