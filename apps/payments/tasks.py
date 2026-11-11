import json
import logging
import urllib.request

from django.conf import settings
from procrastinate import RetryStrategy
from procrastinate.contrib.django import app

from apps.core.context import correlation

from . import reconcile, refunds, services, signing, webhooks
from .providers.fake import FakeProvider

logger = logging.getLogger(__name__)

RETRY = RetryStrategy(max_attempts=8, exponential_wait=2)


@app.task(name="payments.submit_payment", queue="payments", retry=RETRY)
def submit_payment_task(payment_id: int, request_id: str | None = None) -> None:
    with correlation(request_id):
        services.submit_payment(payment_id)


@app.task(name="payments.cancel_stale_payment", queue="payments", retry=RETRY)
def cancel_stale_payment_task(payment_id: int, request_id: str | None = None) -> None:
    with correlation(request_id):
        services.cancel_stale_payment(payment_id)


@app.task(name="payments.process_webhook_event", queue="webhooks", retry=RETRY)
def process_webhook_event_task(webhook_event_id: int, request_id: str | None = None) -> str:
    with correlation(request_id):
        return webhooks.process(webhook_event_id)


@app.task(name="payments.submit_refund", queue="payments", retry=RETRY)
def submit_refund_task(refund_id: int, request_id: str | None = None) -> None:
    with correlation(request_id):
        refunds.submit_refund(refund_id)


@app.periodic(cron="* * * * *", periodic_id="reconcile_stuck_payments")
@app.task(
    name="payments.reconcile_stuck_payments",
    queue="payments",
    queueing_lock="reconcile_stuck_payments",
)
def reconcile_stuck_payments_task(timestamp: int) -> int:
    with correlation():
        return reconcile.reconcile_stuck_payments()


# --- Fake provider internals (would live on the provider's side) ---------------------


@app.task(name="fake_provider.settle", queue="fake_provider")
def fake_provider_settle(payment_id: str) -> None:
    event = FakeProvider().settle(payment_id)
    if event:
        fake_provider_deliver.defer(event=event)


@app.task(name="fake_provider.deliver", queue="fake_provider", retry=RETRY)
def fake_provider_deliver(event: dict) -> int:
    """POST a signed webhook, like a real provider would. Retries on non-2xx."""
    body = json.dumps(event, separators=(",", ":")).encode()
    req = urllib.request.Request(  # noqa: S310 - URL comes from our own settings
        settings.FAKE_PROVIDER_WEBHOOK_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            signing.HEADER: signing.sign(body, settings.PAYMENT_WEBHOOK_SECRET),
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        logger.info("webhook delivered", extra={"event_id": event["id"], "status": resp.status})
        return resp.status
