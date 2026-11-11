"""What if the payment webhook never arrives? Ask the provider.

A periodic job finds payments still `processing` STALE_AFTER after submission and calls
provider.retrieve_payment(). A final status is applied through the *same* code path as
a webhook (apply_payment_update: same locks, same state machine), so:
  - reconciliation and a late webhook can't both apply: the second is a stale no-op
  - a success found this way issues tickets exactly like a webhook would
Last resort, unchanged: after the hold's grace period the order expires and the charge
is cancelled; a success after that is auto-refunded.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.context import bind_order

from .models import Payment, PaymentStatus
from .providers.registry import get_provider
from .webhooks import apply_payment_update

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=2)
BATCH = 200


def reconcile_payment(payment_id: int) -> str:
    payment = Payment.objects.get(pk=payment_id)
    bind_order(payment.order_id)
    if payment.status != PaymentStatus.PROCESSING or not payment.provider_payment_id:
        return "skipped"
    remote = get_provider().retrieve_payment(payment.provider_payment_id)  # outside any lock
    if remote.status == PaymentStatus.PROCESSING:
        return "still_processing"
    obj = {
        "id": remote.id,
        "status": remote.status,
        "amount": remote.amount_cents,
        "currency": remote.currency,
        "metadata": {"payment_id": payment.pk},
    }
    with transaction.atomic():
        _, result = apply_payment_update(f"payment.{remote.status}", obj, source="reconcile")
    logger.warning("payment reconciled without webhook", extra={"result": result})
    return result


def reconcile_stuck_payments(*, now=None) -> int:
    now = now or timezone.now()
    ids = list(
        Payment.objects.filter(
            status=PaymentStatus.PROCESSING,
            provider_payment_id__isnull=False,
            updated_at__lt=now - STALE_AFTER,
        )
        .order_by("updated_at")
        .values_list("id", flat=True)[:BATCH]
    )
    return sum(reconcile_payment(pid) not in ("skipped", "still_processing") for pid in ids)
