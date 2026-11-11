"""Prometheus metrics. Under gunicorn, PROMETHEUS_MULTIPROC_DIR aggregates workers."""

import os

from django.http import HttpResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)

CHECKOUT_LATENCY = Histogram(
    "checkout_latency_seconds",
    "POST /api/orders/ latency by outcome",
    ["outcome"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
CHECKOUT_FAILURES = Counter(
    "checkout_failures_total", "Checkout attempts that did not create an order", ["reason"]
)
WEBHOOK_EVENTS = Counter("webhook_events_total", "Payment webhooks received", ["type", "result"])
PAYMENT_OUTCOMES = Counter("payment_outcomes_total", "Payment final states", ["status"])


def metrics_view(request):
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    else:
        registry = REGISTRY
    return HttpResponse(generate_latest(registry), content_type=CONTENT_TYPE_LATEST)
