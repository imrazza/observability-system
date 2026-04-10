"""
Sample FastAPI application with:
  - Prometheus metrics (/metrics endpoint)
  - Structured JSON logging (shipped via Promtail → Loki)
  - OpenTelemetry distributed tracing (→ Jaeger)
"""

import time
import random
import logging
import json
import os
from datetime import datetime

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

# ── Prometheus ──────────────────────────────────────────────────────────────
from prometheus_client import (
    Counter, Histogram, Gauge, Summary,
    generate_latest, CONTENT_TYPE_LATEST, CollectorRegistry
)

# ── OpenTelemetry ────────────────────────────────────────────────────────────
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource

# ─────────────────────────────────────────────────────────────────────────────
# STRUCTURED LOGGER
# ─────────────────────────────────────────────────────────────────────────────
class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if hasattr(record, "extra"):
            log_entry.update(record.extra)
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger = logging.getLogger("sample-app")
logger.addHandler(handler)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

# ─────────────────────────────────────────────────────────────────────────────
# TRACING SETUP
# ─────────────────────────────────────────────────────────────────────────────
resource = Resource.create({"service.name": "sample-app", "service.version": "1.0.0"})
provider = TracerProvider(resource=resource)

jaeger_exporter = JaegerExporter(
    agent_host_name=os.getenv("JAEGER_AGENT_HOST", "jaeger"),
    agent_port=int(os.getenv("JAEGER_AGENT_PORT", "6831")),
)
provider.add_span_processor(BatchSpanProcessor(jaeger_exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("sample-app")

# ─────────────────────────────────────────────────────────────────────────────
# PROMETHEUS METRICS
# ─────────────────────────────────────────────────────────────────────────────
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"]
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "endpoint"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]
)
ACTIVE_REQUESTS = Gauge(
    "http_active_requests",
    "Number of active HTTP requests"
)
ERROR_COUNT = Counter(
    "http_errors_total",
    "Total HTTP errors",
    ["endpoint", "error_type"]
)
BUSINESS_ORDERS = Counter(
    "business_orders_total",
    "Total orders processed",
    ["status", "product_category"]
)
DB_QUERY_DURATION = Summary(
    "db_query_duration_seconds",
    "Database query duration",
    ["query_type"]
)

# ─────────────────────────────────────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Observable Sample App", version="1.0.0")
FastAPIInstrumentor.instrument_app(app)

PRODUCTS = ["electronics", "clothing", "books", "food", "sports"]
ORDER_STATUSES = ["success", "failed", "pending"]

@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """Track every request with Prometheus."""
    ACTIVE_REQUESTS.inc()
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start

    endpoint = request.url.path
    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=endpoint,
        status_code=response.status_code
    ).inc()
    REQUEST_LATENCY.labels(
        method=request.method,
        endpoint=endpoint
    ).observe(duration)
    ACTIVE_REQUESTS.dec()

    logger.info("HTTP request", extra={
        "extra": {
            "method": request.method,
            "path": endpoint,
            "status": response.status_code,
            "duration_ms": round(duration * 1000, 2),
            "client_ip": request.client.host if request.client else "unknown"
        }
    })
    return response


@app.get("/")
async def root():
    return {"service": "sample-app", "status": "healthy", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/users")
async def list_users():
    with tracer.start_as_current_span("list-users") as span:
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.operation", "SELECT")
        # Simulate DB query
        delay = random.uniform(0.01, 0.15)
        DB_QUERY_DURATION.labels(query_type="SELECT").observe(delay)
        time.sleep(delay)
        count = random.randint(10, 500)
        span.set_attribute("result.count", count)
        logger.info("Users fetched", extra={"extra": {"count": count}})
        return {"users": count, "page": 1}


@app.post("/api/orders")
async def create_order(request: Request):
    with tracer.start_as_current_span("create-order") as span:
        category = random.choice(PRODUCTS)
        status = random.choices(ORDER_STATUSES, weights=[80, 10, 10])[0]
        order_id = f"ORD-{random.randint(10000, 99999)}"

        span.set_attribute("order.id", order_id)
        span.set_attribute("order.category", category)
        span.set_attribute("order.status", status)

        # Simulate order processing
        with tracer.start_as_current_span("validate-payment") as child:
            time.sleep(random.uniform(0.02, 0.08))
            child.set_attribute("payment.method", "card")

        with tracer.start_as_current_span("update-inventory") as child:
            time.sleep(random.uniform(0.01, 0.05))
            child.set_attribute("inventory.updated", True)

        BUSINESS_ORDERS.labels(status=status, product_category=category).inc()

        if status == "failed":
            ERROR_COUNT.labels(endpoint="/api/orders", error_type="order_failed").inc()
            logger.warning("Order failed", extra={
                "extra": {"order_id": order_id, "category": category}
            })
        else:
            logger.info("Order created", extra={
                "extra": {"order_id": order_id, "status": status, "category": category}
            })

        return {"order_id": order_id, "status": status, "category": category}


@app.get("/api/products/{product_id}")
async def get_product(product_id: int):
    with tracer.start_as_current_span("get-product") as span:
        span.set_attribute("product.id", product_id)
        if product_id > 1000:
            ERROR_COUNT.labels(endpoint="/api/products", error_type="not_found").inc()
            logger.error("Product not found", extra={"extra": {"product_id": product_id}})
            raise HTTPException(status_code=404, detail="Product not found")

        delay = random.uniform(0.005, 0.05)
        time.sleep(delay)
        return {
            "id": product_id,
            "name": f"Product {product_id}",
            "category": random.choice(PRODUCTS),
            "price": round(random.uniform(9.99, 499.99), 2)
        }


@app.get("/api/simulate/load")
async def simulate_load():
    """Generates mixed traffic for demo purposes."""
    results = []
    for _ in range(random.randint(3, 8)):
        time.sleep(random.uniform(0.01, 0.1))
        results.append({"simulated": True})
    logger.info("Load simulation completed", extra={"extra": {"requests": len(results)}})
    return {"simulated_requests": len(results)}


@app.get("/api/simulate/error")
async def simulate_error():
    """Intentionally raises an error for alerting demos."""
    ERROR_COUNT.labels(endpoint="/api/simulate/error", error_type="demo_error").inc()
    logger.error("Intentional demo error triggered")
    raise HTTPException(status_code=500, detail="Intentional demo error")


if __name__ == "__main__":
    logger.info("Starting sample-app", extra={"extra": {"port": 8000}})
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
