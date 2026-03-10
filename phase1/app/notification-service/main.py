# notification-service/main.py

import os
import json
import asyncio
from datetime import datetime
from enum import Enum
from typing import Optional

from aiokafka import AIOKafkaConsumer
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
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

SERVICE_NAME  = "notification-service"
REDIS_URL     = os.getenv("REDIS_URL", "redis://redis:6379")
KAFKA_BROKERS = os.getenv("KAFKA_BROKERS", "kafka:9092")
OTLP_ENDPOINT = os.getenv("OTLP_ENDPOINT", "http://otel-collector:4317")

log    = setup_logging(SERVICE_NAME)
tracer = setup_tracing(SERVICE_NAME, OTLP_ENDPOINT)
meter  = setup_metrics(SERVICE_NAME, OTLP_ENDPOINT)

# ── Business Metrics ──────────────────────────────────────────────────────────

request_counter = make_request_counter(SERVICE_NAME)
latency_hist    = make_request_latency(SERVICE_NAME)
active_requests = make_active_requests(SERVICE_NAME)

notifications_sent = Counter(
    "notifications_sent_total",
    "Total notifications sent",
    ["channel", "event_type", "status"],
)
notification_latency = Histogram(
    "notification_delivery_duration_seconds",
    "Time to deliver a notification",
    ["channel"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0],
)
kafka_events_consumed = Counter(
    "kafka_events_consumed_total",
    "Total Kafka events consumed",
    ["topic", "event_type"],
)
kafka_consumer_lag = Gauge(
    "kafka_consumer_lag_messages",
    "Estimated consumer lag in messages",
    ["topic"],
)
notification_queue_depth = Gauge(
    "notification_queue_depth", "Pending notifications in queue",
)
failed_deliveries = Counter(
    "notification_delivery_failures_total",
    "Failed notification deliveries",
    ["channel", "reason"],
)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Notification Service", version="1.0.0")
app.add_route("/metrics", metrics_endpoint)
instrument_app(app, SERVICE_NAME)

consumer: Optional[AIOKafkaConsumer] = None

@app.on_event("startup")
async def startup():
    global consumer
    consumer = AIOKafkaConsumer(
        "orders", "payments",
        bootstrap_servers=KAFKA_BROKERS,
        group_id="notification-service",
        auto_offset_reset="earliest",
        value_deserializer=lambda m: json.loads(m.decode()),
    )
    await consumer.start()
    asyncio.create_task(consume_events())
    log.info("notification-service started, consuming from [orders, payments]")

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
    if consumer is None:
        raise HTTPException(status_code=503, detail="Kafka consumer not ready")
    return {"status": "ready"}

@app.post("/notifications/send")
async def send_manual_notification(payload: dict):
    """Manually trigger a notification — used by remediation workflows."""
    with tracer.start_as_current_span("send_manual_notification") as span:
        span.set_attribute("notification.channel", payload.get("channel", "slack"))
        result = await deliver_notification(
            channel=payload.get("channel", "slack"),
            event_type="manual",
            data=payload,
        )
        return {"status": "sent", "result": result}

@app.get("/notifications/stats")
async def notification_stats():
    return {
        "service": SERVICE_NAME,
        "consumer_topics": ["orders", "payments"],
        "status": "running" if consumer else "stopped",
    }

# ── Kafka Consumer ────────────────────────────────────────────────────────────

async def consume_events():
    """
    Main event loop — consumes from Kafka and dispatches notifications.
    Runs as a background task for the lifetime of the service.
    """
    log.info("kafka consumer loop started")
    async for msg in consumer:
        topic      = msg.topic
        event_data = msg.value
        event_type = event_data.get("event", "unknown")

        kafka_events_consumed.labels(topic=topic, event_type=event_type).inc()
        notification_queue_depth.inc()

        log.info("kafka event received",
            topic=topic,
            event_type=event_type,
            offset=msg.offset,
            partition=msg.partition,
        )

        with tracer.start_as_current_span(f"process_{event_type}") as span:
            span.set_attribute("kafka.topic", topic)
            span.set_attribute("kafka.offset", msg.offset)
            span.set_attribute("event.type", event_type)

            try:
                await route_event(event_type, event_data)
            except Exception as e:
                log.error("failed to process event",
                    event_type=event_type,
                    error=str(e),
                    topic=topic,
                )
                span.set_attribute("error", True)
            finally:
                notification_queue_depth.dec()


async def route_event(event_type: str, data: dict):
    """Route events to the correct notification handler."""
    handlers = {
        "order.confirmed":   handle_order_confirmed,
        "payment.processed": handle_payment_processed,
        "payment.refunded":  handle_payment_refunded,
    }
    handler = handlers.get(event_type)
    if handler:
        await handler(data)
    else:
        log.warning("no handler for event type", event_type=event_type)


async def handle_order_confirmed(data: dict):
    await deliver_notification(
        channel="email",
        event_type="order.confirmed",
        data={
            "to":       f"{data['user_id']}@example.com",
            "subject":  f"Order {data['order_id']} confirmed",
            "body":     f"Your order for ${data['total']:.2f} has been confirmed.",
        },
    )


async def handle_payment_processed(data: dict):
    await deliver_notification(
        channel="email",
        event_type="payment.processed",
        data={
            "to":      f"user@example.com",
            "subject": "Payment received",
            "body":    f"Payment of ${data['amount']:.2f} processed for order {data['order_id']}.",
        },
    )


async def handle_payment_refunded(data: dict):
    # Refunds get both email and SMS
    await deliver_notification(
        channel="email",
        event_type="payment.refunded",
        data={"body": f"Refund of ${data['amount']:.2f} issued for payment {data['payment_id']}."},
    )
    await deliver_notification(
        channel="sms",
        event_type="payment.refunded",
        data={"body": f"Refund of ${data['amount']:.2f} is on its way."},
    )


async def deliver_notification(channel: str, event_type: str, data: dict) -> dict:
    """
    Deliver a notification via the specified channel.
    In production: integrates with SendGrid (email), Twilio (SMS), Slack webhooks.
    """
    import time
    import random
    start = time.perf_counter()

    # Simulate delivery latency per channel
    delays = {"email": 0.05, "sms": 0.03, "slack": 0.01}
    await asyncio.sleep(delays.get(channel, 0.05))

    # Simulate occasional failures
    if random.random() < 0.01:
        failed_deliveries.labels(channel=channel, reason="gateway_timeout").inc()
        notifications_sent.labels(channel=channel, event_type=event_type, status="failed").inc()
        log.error("notification delivery failed",
            channel=channel,
            event_type=event_type,
        )
        return {"status": "failed"}

    duration = time.perf_counter() - start
    notification_latency.labels(channel=channel).observe(duration)
    notifications_sent.labels(channel=channel, event_type=event_type, status="sent").inc()

    log.info("notification delivered",
        channel=channel,
        event_type=event_type,
        duration_ms=round(duration * 1000, 2),
    )

    return {"status": "sent", "channel": channel, "duration_ms": round(duration * 1000, 2)}
