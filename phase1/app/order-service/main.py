# order-service/main.py

import os
import uuid
import asyncio
from datetime import datetime
from enum import Enum
from typing import Optional

import httpx
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
    instrument_app, ObservabilityMiddleware, metrics_endpoint,
    make_request_counter, make_request_latency, make_active_requests,
)

# ── Config ────────────────────────────────────────────────────────────────────

SERVICE_NAME   = "order-service"
REDIS_URL      = os.getenv("REDIS_URL", "redis://redis:6379")
KAFKA_BROKERS  = os.getenv("KAFKA_BROKERS", "kafka:9092")
PAYMENT_URL    = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8001")
OTLP_ENDPOINT  = os.getenv("OTLP_ENDPOINT", "http://otel-collector:4317")

# ── Observability setup ───────────────────────────────────────────────────────

log    = setup_logging(SERVICE_NAME)
tracer = setup_tracing(SERVICE_NAME, OTLP_ENDPOINT)
meter  = setup_metrics(SERVICE_NAME, OTLP_ENDPOINT)

# ── Business Metrics (this is what separates us from basic monitoring) ─────────

request_counter  = make_request_counter(SERVICE_NAME, extra_labels=["order_status"])
latency_hist     = make_request_latency(SERVICE_NAME)
active_requests  = make_active_requests(SERVICE_NAME)

# Business-aware metrics — not just infra
orders_created = Counter(
    "orders_created_total", "Total orders created",
    ["region", "item_category"],
)
orders_failed = Counter(
    "orders_failed_total", "Total orders that failed",
    ["reason"],
)
order_value = Histogram(
    "order_value_dollars",
    "Distribution of order values in dollars",
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000],
)
payment_latency = Histogram(
    "order_payment_call_duration_seconds",
    "Latency of calls to payment-service",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)
pending_orders = Gauge("orders_pending_total", "Orders currently in pending state")
kafka_publish_errors = Counter("kafka_publish_errors_total", "Failed Kafka publishes", ["topic"])

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Order Service", version="1.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_route("/metrics", metrics_endpoint)

instrument_app(app, SERVICE_NAME)

# ── Models ────────────────────────────────────────────────────────────────────

class OrderStatus(str, Enum):
    PENDING   = "pending"
    CONFIRMED = "confirmed"
    FAILED    = "failed"
    CANCELLED = "cancelled"

class CreateOrderRequest(BaseModel):
    user_id:       str             = Field(..., description="User placing the order")
    items:         list[dict]      = Field(..., description="List of {item_id, quantity, price}")
    region:        str             = Field(default="us-east-1")
    item_category: str             = Field(default="general")

class OrderResponse(BaseModel):
    order_id:   str
    user_id:    str
    status:     OrderStatus
    total:      float
    created_at: str
    trace_id:   Optional[str] = None

# ── Dependencies ──────────────────────────────────────────────────────────────

async def get_redis():
    client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()

# ── Kafka producer (module-level, shared) ─────────────────────────────────────

producer: Optional[AIOKafkaProducer] = None

@app.on_event("startup")
async def startup():
    global producer
    producer = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BROKERS,
        value_serializer=lambda v: str(v).encode(),
    )
    await producer.start()
    log.info("order-service started", kafka_brokers=KAFKA_BROKERS)

@app.on_event("shutdown")
async def shutdown():
    if producer:
        await producer.stop()
    log.info("order-service shutting down gracefully")

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health/live")
async def liveness():
    return {"status": "alive"}

@app.get("/health/ready")
async def readiness(r: redis.Redis = Depends(get_redis)):
    try:
        await r.ping()
        return {"status": "ready"}
    except Exception as e:
        log.error("readiness check failed", error=str(e))
        raise HTTPException(status_code=503, detail="Redis not reachable")

@app.post("/orders", response_model=OrderResponse, status_code=201)
async def create_order(
    req: CreateOrderRequest,
    background_tasks: BackgroundTasks,
    r: redis.Redis = Depends(get_redis),
):
    order_id = str(uuid.uuid4())
    total    = sum(item.get("price", 0) * item.get("quantity", 1) for item in req.items)

    log.info("creating order",
        order_id=order_id,
        user_id=req.user_id,
        total=total,
        item_count=len(req.items),
    )

    with tracer.start_as_current_span("create_order") as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("order.total", total)
        span.set_attribute("order.user_id", req.user_id)
        span.set_attribute("order.item_count", len(req.items))

        # Store order in Redis (pending state)
        order_data = {
            "order_id":   order_id,
            "user_id":    req.user_id,
            "status":     OrderStatus.PENDING,
            "total":      total,
            "region":     req.region,
            "created_at": datetime.utcnow().isoformat(),
        }
        await r.hset(f"order:{order_id}", mapping={k: str(v) for k, v in order_data.items()})
        await r.expire(f"order:{order_id}", 86400)  # 24h TTL
        pending_orders.inc()

        # Call payment service — track latency as business metric
        import time
        payment_start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                payment_resp = await client.post(
                    f"{PAYMENT_URL}/payments",
                    json={"order_id": order_id, "amount": total, "user_id": req.user_id},
                )
                payment_resp.raise_for_status()
        except httpx.TimeoutException:
            orders_failed.labels(reason="payment_timeout").inc()
            await r.hset(f"order:{order_id}", "status", OrderStatus.FAILED)
            pending_orders.dec()
            log.error("payment service timeout", order_id=order_id)
            span.set_attribute("error", True)
            raise HTTPException(status_code=504, detail="Payment service timeout")
        except Exception as e:
            orders_failed.labels(reason="payment_error").inc()
            await r.hset(f"order:{order_id}", "status", OrderStatus.FAILED)
            pending_orders.dec()
            log.error("payment service error", order_id=order_id, error=str(e))
            span.set_attribute("error", True)
            raise HTTPException(status_code=502, detail="Payment service error")
        finally:
            payment_latency.observe(time.perf_counter() - payment_start)

        # Payment succeeded — update order
        await r.hset(f"order:{order_id}", "status", OrderStatus.CONFIRMED)
        pending_orders.dec()

        # Record business metrics
        orders_created.labels(region=req.region, item_category=req.item_category).inc()
        order_value.observe(total)

        # Publish event to Kafka asynchronously
        background_tasks.add_task(publish_order_event, order_id, req.user_id, total)

        log.info("order confirmed",
            order_id=order_id,
            user_id=req.user_id,
            total=total,
            status="confirmed",
        )

        return OrderResponse(
            order_id=order_id,
            user_id=req.user_id,
            status=OrderStatus.CONFIRMED,
            total=total,
            created_at=order_data["created_at"],
        )


@app.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(order_id: str, r: redis.Redis = Depends(get_redis)):
    with tracer.start_as_current_span("get_order") as span:
        span.set_attribute("order.id", order_id)
        data = await r.hgetall(f"order:{order_id}")
        if not data:
            raise HTTPException(status_code=404, detail="Order not found")
        return OrderResponse(**data)


@app.delete("/orders/{order_id}")
async def cancel_order(order_id: str, r: redis.Redis = Depends(get_redis)):
    with tracer.start_as_current_span("cancel_order") as span:
        span.set_attribute("order.id", order_id)
        exists = await r.exists(f"order:{order_id}")
        if not exists:
            raise HTTPException(status_code=404, detail="Order not found")
        await r.hset(f"order:{order_id}", "status", OrderStatus.CANCELLED)
        log.info("order cancelled", order_id=order_id)
        return {"order_id": order_id, "status": "cancelled"}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def publish_order_event(order_id: str, user_id: str, total: float):
    """Publish order.confirmed event to Kafka for downstream consumers."""
    try:
        import json
        event = json.dumps({
            "event":    "order.confirmed",
            "order_id": order_id,
            "user_id":  user_id,
            "total":    total,
            "ts":       datetime.utcnow().isoformat(),
        })
        await producer.send_and_wait("orders", event.encode())
        log.info("order event published", order_id=order_id, topic="orders")
    except Exception as e:
        kafka_publish_errors.labels(topic="orders").inc()
        log.error("failed to publish order event", order_id=order_id, error=str(e))
