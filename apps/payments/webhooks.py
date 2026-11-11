"""Webhook intake and processing.

Intake (HTTP):   verify signature -> dedupe by event id -> store -> enqueue -> 200.
Processing (job): lock event -> lock Order -> lock Payment -> apply a *valid* transition
                  or record why the event was ignored. Running it twice is a no-op.

Out-of-order / duplicate safety comes from three layers:
  1. unique (provider, event_id)              - exact redeliveries
  2. WebhookEvent.status under a row lock     - the same event processed concurrently
  3. state machines (terminal states final)   - different events with stale info
"""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.audit import audit
from apps.core.context import bind_order, get_request_id
from apps.core.errors import capture_exception
from apps.core.metrics import PAYMENT_OUTCOMES, WEBHOOK_EVENTS
from apps.orders.issuance import issue_tickets
from apps.orders.models import ORDER_MACHINE, Order, OrderStatus

from . import signing
from .models import (
    PAYMENT_MACHINE,
    REFUND_MACHINE,
    Payment,
    PaymentStatus,
    Refund,
    RefundStatus,
    WebhookEvent,
    WebhookStatus,
)

logger = logging.getLogger(__name__)

PAYMENT_EVENT_STATUS = {
    "payment.processing": PaymentStatus.PROCESSING,
    "payment.succeeded": PaymentStatus.SUCCEEDED,
    "payment.failed": PaymentStatus.FAILED,
    "payment.canceled": PaymentStatus.CANCELED,
}


class BadWebhook(Exception):
    pass


def receive(body: bytes, signature_header: str, provider: str = "fake") -> tuple[bool, str]:
    """Returns (created, event_id). Raises BadWebhook on signature/format problems."""
    try:
        signing.verify(
            body,
            signature_header,
            settings.PAYMENT_WEBHOOK_SECRET,
            settings.PAYMENT_WEBHOOK_TOLERANCE_SECONDS,
        )
    except signing.SignatureError as exc:
        WEBHOOK_EVENTS.labels(type="unknown", result="bad_signature").inc()
        raise BadWebhook(str(exc)) from exc
    try:
        payload = json.loads(body)
        event_id, event_type = payload["id"], payload["type"]
        payload["data"]["object"]
    except (ValueError, KeyError, TypeError) as exc:
        raise BadWebhook("malformed payload") from exc

    from .tasks import process_webhook_event_task

    with transaction.atomic():
        try:
            with transaction.atomic():
                ev = WebhookEvent.objects.create(
                    provider=provider, event_id=event_id, type=event_type, payload=payload
                )
        except IntegrityError:
            WEBHOOK_EVENTS.labels(type=event_type, result="duplicate").inc()
            return False, event_id
        process_webhook_event_task.defer(webhook_event_id=ev.pk, request_id=get_request_id())
    WEBHOOK_EVENTS.labels(type=event_type, result="accepted").inc()
    return True, event_id


def process(webhook_event_id: int) -> str:
    with transaction.atomic():
        ev = WebhookEvent.objects.select_for_update().get(pk=webhook_event_id)
        if ev.status != WebhookStatus.RECEIVED:
            return f"already_{ev.status}"
        obj = ev.payload["data"]["object"]
        if ev.type in PAYMENT_EVENT_STATUS:
            status, result = _handle_payment_event(ev.type, obj)
        elif ev.type == "refund.succeeded":
            status, result = _handle_refund_succeeded(obj)
        else:
            status, result = WebhookStatus.IGNORED, "unhandled_type"
        ev.status, ev.result, ev.processed_at = status, result, timezone.now()
        ev.save(update_fields=["status", "result", "processed_at"])
        logger.info("webhook processed", extra={"event_id": ev.event_id, "result": result})
        return result


def _find_payment_id(obj: dict) -> int | None:
    pid = (obj.get("metadata") or {}).get("payment_id")
    if pid:
        return int(pid)
    return (
        Payment.objects.filter(provider_payment_id=obj.get("id"))
        .values_list("pk", flat=True)
        .first()
    )


def _handle_payment_event(event_type: str, obj: dict) -> tuple[str, str]:
    payment_id = _find_payment_id(obj)
    order_id = Payment.objects.filter(pk=payment_id).values_list("order_id", flat=True).first()
    if order_id is None:
        return WebhookStatus.IGNORED, "unknown_payment"
    bind_order(order_id)
    # Lock order first, then payment: the global lock order.
    order = Order.objects.select_for_update().get(pk=order_id)
    payment = Payment.objects.select_for_update().get(pk=payment_id)

    if not payment.provider_payment_id:  # webhook overtook the submit job's write
        payment.provider_payment_id = obj["id"]
        payment.save(update_fields=["provider_payment_id"])
    elif payment.provider_payment_id != obj["id"]:
        return WebhookStatus.IGNORED, "provider_id_mismatch"

    target = PAYMENT_EVENT_STATUS[event_type]
    if target == payment.status == PaymentStatus.PROCESSING:
        return WebhookStatus.IGNORED, "no_change"
    if not PAYMENT_MACHINE.can(payment.status, target):
        logger.warning(
            "stale or out-of-order payment event",
            extra={"current": payment.status, "event_status": target},
        )
        return WebhookStatus.IGNORED, f"stale:{payment.status}->{target}"

    if target == PaymentStatus.SUCCEEDED and (
        obj.get("amount") != payment.amount_cents or obj.get("currency") != payment.currency
    ):
        capture_exception(RuntimeError(f"amount mismatch on payment {payment.pk}"))
        return WebhookStatus.IGNORED, "amount_mismatch"

    if target == PaymentStatus.FAILED:
        payment.failure_reason = obj.get("failure_code", "failed")
    PAYMENT_MACHINE.apply(payment, target, extra_fields=["failure_reason"], source="webhook")
    PAYMENT_OUTCOMES.labels(status=target).inc()

    if target != PaymentStatus.SUCCEEDED:
        return WebhookStatus.PROCESSED, f"payment_{target}"
    if order.status == OrderStatus.PENDING:
        _confirm_order(order)
        return WebhookStatus.PROCESSED, "order_paid"
    # Money arrived for an order that already expired/cancelled: its seats may have been
    # resold, so never over-commit inventory. Give the money back.
    _auto_refund_late_payment(payment)
    return WebhookStatus.PROCESSED, f"late_payment_refunded:{order.status}"


def _confirm_order(order: Order) -> None:
    from apps.orders.tasks import send_order_confirmation_task

    order.paid_at = timezone.now()
    ORDER_MACHINE.apply(order, OrderStatus.PAID, extra_fields=["paid_at"], source="webhook")
    issue_tickets(order)
    send_order_confirmation_task.defer(order_id=order.pk, request_id=get_request_id())


def _auto_refund_late_payment(payment: Payment) -> Refund:
    from .refunds import create_refund_record

    refund = create_refund_record(payment, payment.amount_cents, reason="late_payment")
    audit(payment, "auto_refund", amount_cents=payment.amount_cents)
    return refund


def _handle_refund_succeeded(obj: dict) -> tuple[str, str]:
    refund_id = (obj.get("metadata") or {}).get("refund_id")
    refund = Refund.objects.filter(pk=refund_id).select_related("payment").first()
    if refund is None:
        return WebhookStatus.IGNORED, "unknown_refund"
    bind_order(refund.payment.order_id)
    Order.objects.select_for_update().get(pk=refund.payment.order_id)  # lock order first
    refund = Refund.objects.select_for_update().get(pk=refund.pk)
    if not REFUND_MACHINE.can(refund.status, RefundStatus.SUCCEEDED):
        return WebhookStatus.IGNORED, f"stale:{refund.status}"
    refund.provider_refund_id = refund.provider_refund_id or obj["id"]
    REFUND_MACHINE.apply(refund, RefundStatus.SUCCEEDED, extra_fields=["provider_refund_id"])
    return WebhookStatus.PROCESSED, "refund_succeeded"
