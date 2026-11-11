"""Refund bookkeeping shared by organizer refunds (step 9) and late-payment auto-refunds.

Money moves in two phases, like payments:
  1. In our transaction: bump Payment.refunded_cents (CHECK <= amount_cents), create a
     pending Refund, enqueue submission. Nothing external happens under the lock.
  2. Worker: call provider.refund with the Refund's idempotency key; webhook finalises.
"""

from __future__ import annotations

import uuid

from django.db.models import F

from apps.core.audit import audit
from apps.core.context import get_request_id

from .models import Payment, Refund
from .providers.registry import get_provider


def create_refund_record(
    payment: Payment, amount_cents: int, *, reason: str, actor=None, tickets=()
) -> Refund:
    """Caller holds the Order and Payment row locks."""
    from .tasks import submit_refund_task

    Payment.objects.filter(pk=payment.pk).update(refunded_cents=F("refunded_cents") + amount_cents)
    payment.refresh_from_db(fields=["refunded_cents"])
    refund = Refund.objects.create(
        payment=payment,
        amount_cents=amount_cents,
        reason=reason,
        provider_idempotency_key=f"re_{uuid.uuid4().hex}",
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    if tickets:
        refund.tickets.set(tickets)
    audit(
        refund,
        "created",
        to_state=refund.status,
        actor=actor,
        amount_cents=amount_cents,
        reason=reason,
    )
    submit_refund_task.defer(refund_id=refund.pk, request_id=get_request_id())
    return refund


def submit_refund(refund_id: int) -> None:
    refund = Refund.objects.select_related("payment").get(pk=refund_id)
    if refund.provider_refund_id or refund.status != "pending":
        return
    result = get_provider().refund(
        payment_id=refund.payment.provider_payment_id,
        amount_cents=refund.amount_cents,
        idempotency_key=refund.provider_idempotency_key,
        metadata={"refund_id": refund.pk, "payment_id": refund.payment_id},
    )
    Refund.objects.filter(pk=refund.pk, provider_refund_id__isnull=True).update(
        provider_refund_id=result.id
    )
