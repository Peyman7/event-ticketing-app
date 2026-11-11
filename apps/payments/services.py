"""Payment lifecycle. Lock order: Order -> Payment (see apps/orders/inventory.py)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.audit import audit
from apps.core.context import bind_order, get_request_id
from apps.orders.models import Order, OrderStatus

from .models import PAYMENT_MACHINE, Payment, PaymentStatus
from .providers.base import ProviderError
from .providers.registry import get_provider

logger = logging.getLogger(__name__)


class PaymentRejected(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409):
        self.code, self.status_code = code, status_code
        super().__init__(message)


def in_flight_payment(order: Order) -> Payment | None:
    return order.payments.filter(status=PaymentStatus.PROCESSING).first()


def may_expire(order: Order, now: datetime) -> tuple[bool, Payment | None]:
    """A hold with a charge in flight gets PAYMENT_GRACE_MINUTES extra before expiring,
    so a slow-but-successful payment doesn't lose its tickets."""
    payment = in_flight_payment(order)
    if payment is None:
        return True, None
    grace_end = order.expires_at + timedelta(minutes=settings.PAYMENT_GRACE_MINUTES)
    return now >= grace_end, payment


def start_payment(order_id: int, buyer, payment_method: str) -> Payment:
    """Create a processing Payment and enqueue its submission, in the caller's txn.

    No provider call happens here: the HTTP request never waits on (or holds locks
    during) an external call. The worker submits it with a provider idempotency key.
    """
    from .tasks import submit_payment_task

    order = Order.objects.select_for_update().get(pk=order_id, buyer=buyer)
    bind_order(order.pk)
    if order.status != OrderStatus.PENDING:
        raise PaymentRejected("invalid_state", f"Order is {order.status}.")
    if order.expires_at <= timezone.now():
        raise PaymentRejected("hold_expired", "The ticket hold has expired.")
    if order.total_cents == 0:
        raise PaymentRejected("free_order", "Free orders need no payment.", 400)
    if order.payments.filter(status=PaymentStatus.SUCCEEDED).exists():
        raise PaymentRejected("already_paid", "Order is already paid.")
    if in_flight_payment(order):
        raise PaymentRejected("payment_in_flight", "A payment for this order is processing.")

    payment = Payment.objects.create(
        order=order,
        provider=get_provider().name,
        provider_idempotency_key=f"pay_{uuid.uuid4().hex}",
        payment_method=payment_method,
        amount_cents=order.total_cents,
        currency=order.currency,
    )
    audit(payment, "created", to_state=payment.status, actor=buyer, amount_cents=order.total_cents)
    submit_payment_task.defer(payment_id=payment.pk, request_id=get_request_id())
    return payment


def submit_payment(payment_id: int) -> None:
    """Worker side: call the provider. Safe to retry (same idempotency key)."""
    payment = Payment.objects.select_related("order").get(pk=payment_id)
    bind_order(payment.order_id)
    if payment.status != PaymentStatus.PROCESSING or payment.provider_payment_id:
        return
    result = get_provider().create_payment(
        amount_cents=payment.amount_cents,
        currency=payment.currency,
        payment_method=payment.payment_method,
        idempotency_key=payment.provider_idempotency_key,
        metadata={"payment_id": payment.pk, "order_id": payment.order_id},
    )
    # Conditional update, no lock needed: only ever goes NULL -> id.
    Payment.objects.filter(pk=payment.pk, provider_payment_id__isnull=True).update(
        provider_payment_id=result.id
    )
    logger.info("payment submitted", extra={"provider_payment_id": result.id})


def cancel_stale_payment(payment_id: int) -> None:
    """After the grace period the order expired; ask the provider to cancel the charge.
    If it already succeeded, the success webhook triggers an automatic refund instead."""
    payment = Payment.objects.get(pk=payment_id)
    if payment.status != PaymentStatus.PROCESSING:
        return
    if not payment.provider_payment_id:
        # Never reached the provider (submission kept failing): fail it locally.
        with transaction.atomic():
            locked = Payment.objects.select_for_update().get(pk=payment_id)
            if locked.status == PaymentStatus.PROCESSING and not locked.provider_payment_id:
                locked.failure_reason = "never_submitted"
                PAYMENT_MACHINE.apply(locked, PaymentStatus.FAILED, extra_fields=["failure_reason"])
        return
    try:
        get_provider().cancel_payment(payment.provider_payment_id)
    except ProviderError:
        logger.warning("provider cancel failed; will rely on webhook/reconciliation")
        raise
