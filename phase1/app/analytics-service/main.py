# analytics-service/main.py

import os
import json
import asyncio
from datetime import datetime, date
from typing import Optional

from aiokafka import AIOKafkaConsumer
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
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

SERVICE_NAME  = "analytics-service"
REDIS_URL     = os.getenv("REDIS_URL", "redis://redis:6379")
KAFKA_BROKERS = os.getenv("KAFKA_BROKERS", "kafka:9092")
OTLP_ENDPOINT = os.getenv("OTLP_ENDPOINT", "http://otel-collector:4317")

log    = setup_logging(SERVICE_NAME)
tracer = setup_tracing(SERVICE_NAME, OTLP_ENDPOINT)
meter  = setup_metrics(SERVICE_NAME, OTLP_ENDPOINT)

# ── Business Metrics — the CFO dashboard layer ────────────────────────────────

request_counter = make_request_counter(SERVICE_NAME)
latency_hist    = make_request_latency(SERVICE_NAME)
active_requests = make_active_requests(SERVICE_NAME)

# Real-time business KPIs exposed as Prometheus metrics
# These feed the Executive SLO dashboard in Grafana
gmv_today = Gauge(
    "analytics_gmv_today_dollars",
    "Gross merchandise value today in dollars",
)
orders_today = Gauge(
    "analytics_orders_today_total",
    "Total orders placed today",
)
average_order_value = Gauge(
    "analytics_average_order_value_dollars",
    "Rolling average order value in dollars",
)
conversion_rate = Gauge(
    "analytics_conversion_rate_percent",
    "Order conversion rate as a percentage",
)
refund_rate = Gauge(
    "analytics_refund_rate_percent",
    "Payment refund rate as a percentage",
)
events_processed = Counter(
    "analytics_events_processed_total",
    "Total events processed by analytics service",
    ["event_type"],
)
processing_latency = Histogram(
    "analytics_event_processing_duration_seconds",
    "Time to process and aggregate an event",
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5],
)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Analytics Service", version="1.0.0")
app.add_route("/metrics", metrics_endpoint)
instrument_app(app, SERVICE_NAME)

consumer: Optional[AIOKafkaConsumer] = None

@app.on_event("startup")
async def startup():
    global consumer
    consumer = AIOKafkaConsumer(
        "orders", "payments",
        bootstrap_servers=KAFKA_BROKERS,
        group_id="analytics-service",
        auto_offset_reset="earliest",
        value_deserializer=lambda m: json.loads(m.decode()),
    )
    await consumer.start()
    asyncio.create_task(consume_and_aggregate())
    log.info("analytics-service started")

@app.on_event("shutdown")
async def shutdown():
    if consumer:
        await consumer.stop()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health/live")
async def liveness():
    return {"status": "alive"}

@app.get("/health/ready")
async def readiness():
    return {"status": "ready"}

@app.get("/analytics/summary")
async def get_summary(r: redis.Redis = Depends(get_redis)):
    """Real-time business summary — used by the executive dashboard."""
    with tracer.start_as_current_span("get_summary"):
        today = date.today().isoformat()
        keys = [
            f"analytics:orders:{today}",
            f"analytics:gmv:{today}",
            f"analytics:payments:{today}",
            f"analytics:refunds:{today}",
        ]
        values = await r.mget(*keys)
        orders     = int(values[0] or 0)
        gmv        = float(values[1] or 0)
        payments   = int(values[2] or 0)
        refunds    = int(values[3] or 0)
        avg_order  = gmv / orders if orders > 0 else 0
        ref_rate   = (refunds / payments * 100) if payments > 0 else 0

        return {
            "date":              today,
            "orders_today":      orders,
            "gmv_today":         round(gmv, 2),
            "average_order_value": round(avg_order, 2),
            "refund_rate_pct":   round(ref_rate, 2),
            "payments_today":    payments,
            "refunds_today":     refunds,
        }

@app.get("/analytics/hourly")
async def get_hourly(r: redis.Redis = Depends(get_redis)):
    """Hourly GMV breakdown for the last 24 hours."""
    with tracer.start_as_current_span("get_hourly"):
        now   = datetime.utcnow()
        hours = []
        for h in range(23, -1, -1):
            hour_key = f"analytics:gmv:hour:{now.strftime('%Y-%m-%d')}:{(now.hour - h) % 24:02d}"
            val = await r.get(hour_key)
            hours.append({
                "hour":  f"{(now.hour - h) % 24:02d}:00",
                "gmv":   float(val or 0),
            })
        return {"hourly": hours}

# ── Kafka Consumer + Aggregation ──────────────────────────────────────────────

async def consume_and_aggregate():
    """Consume all events and maintain real-time aggregates in Redis."""
    log.info("analytics consumer loop started")
    async for msg in consumer:
        import time
        start = time.perf_counter()
        event_data = msg.value
        event_type = event_data.get("event", "unknown")

        events_processed.labels(event_type=event_type).inc()

        with tracer.start_as_current_span(f"aggregate_{event_type}") as span:
            span.set_attribute("event.type", event_type)
            span.set_attribute("kafka.topic", msg.topic)

            try:
                await aggregate_event(event_type, event_data)
                processing_latency.observe(time.perf_counter() - start)
            except Exception as e:
                log.error("aggregation failed", event_type=event_type, error=str(e))


async def aggregate_event(event_type: str, data: dict):
    """Update Redis aggregates and Prometheus gauges for each event."""
    async for r in get_redis():
        today = date.today().isoformat()
        now   = datetime.utcnow()
        hour  = now.strftime("%H")

        if event_type == "order.confirmed":
            total = float(data.get("total", 0))
            await r.incr(f"analytics:orders:{today}")
            await r.incrbyfloat(f"analytics:gmv:{today}", total)
            await r.incrbyfloat(f"analytics:gmv:hour:{today}:{hour}", total)

            # Expire keys at midnight + 1 hour buffer
            for key in [
                f"analytics:orders:{today}",
                f"analytics:gmv:{today}",
                f"analytics:gmv:hour:{today}:{hour}",
            ]:
                await r.expire(key, 90000)  # 25 hours

            # Update Prometheus gauges
            orders = int(await r.get(f"analytics:orders:{today}") or 0)
            gmv    = float(await r.get(f"analytics:gmv:{today}") or 0)
            orders_today.set(orders)
            gmv_today.set(gmv)
            average_order_value.set(gmv / orders if orders > 0 else 0)

            log.info("order aggregated", total=total, gmv_today=gmv, orders_today=orders)

        elif event_type == "payment.processed":
            await r.incr(f"analytics:payments:{today}")
            await r.expire(f"analytics:payments:{today}", 90000)

            payments = int(await r.get(f"analytics:payments:{today}") or 0)
            refunds  = int(await r.get(f"analytics:refunds:{today}") or 0)
            if payments > 0:
                refund_rate.set(refunds / payments * 100)

        elif event_type == "payment.refunded":
            amount = float(data.get("amount", 0))
            await r.incr(f"analytics:refunds:{today}")
            await r.expire(f"analytics:refunds:{today}", 90000)

            payments = int(await r.get(f"analytics:payments:{today}") or 0)
            refunds  = int(await r.get(f"analytics:refunds:{today}") or 0)
            if payments > 0:
                refund_rate.set(refunds / payments * 100)

            log.info("refund aggregated", amount=amount)


async def get_redis():
    client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()
