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
from apps.core.context import bind_order, get_request_id

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


class RefundRejected(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409):
        self.code, self.status_code = code, status_code
        super().__init__(message)


def refund_tickets(order_id: int, ticket_ids: list[int], actor) -> Refund | None:
    """Organizer refund of specific tickets. Runs in the caller's transaction.

    Lock order: Order -> Payment -> Ticket -> TicketType (release).
    Returns None when the selected tickets were free (nothing to send to the provider).
    """
    from apps.orders.inventory import release
    from apps.orders.models import ORDER_MACHINE, Order, OrderStatus, Ticket, TicketStatus

    from .models import PaymentStatus

    order = Order.objects.select_for_update().get(pk=order_id)
    bind_order(order.pk)
    if order.status not in (OrderStatus.PAID, OrderStatus.PARTIALLY_REFUNDED):
        raise RefundRejected("invalid_state", f"Order is {order.status}.")
    payment = Payment.objects.select_for_update().get(order=order, status=PaymentStatus.SUCCEEDED)

    wanted = sorted(set(ticket_ids))
    tickets = list(
        Ticket.objects.select_for_update()
        .filter(pk__in=wanted, order_item__order=order)
        .order_by("id")
    )
    if len(tickets) != len(wanted) or not wanted:
        raise RefundRejected("unknown_ticket", "Tickets must belong to this order.", 400)
    for t in tickets:
        if t.status == TicketStatus.CHECKED_IN:
            raise RefundRejected("ticket_checked_in", f"Ticket {t.pk} was already used.")
        if t.status == TicketStatus.REFUNDED:
            raise RefundRejected("already_refunded", f"Ticket {t.pk} was already refunded.")

    amount = sum(t.price_cents for t in tickets)
    if payment.refunded_cents + amount > payment.amount_cents:  # CHECK constraint backs this
        raise RefundRejected("over_refund", "Refund would exceed the amount paid.")

    Ticket.objects.filter(pk__in=[t.pk for t in tickets]).update(status=TicketStatus.REFUNDED)
    counts: dict[int, int] = {}
    for t in tickets:
        counts[t.ticket_type_id] = counts.get(t.ticket_type_id, 0) + 1
    release(counts)  # seats go back on sale

    refund = None
    if amount:
        refund = create_refund_record(
            payment, amount, reason="requested_by_organizer", actor=actor, tickets=tickets
        )
    still_live = Ticket.objects.filter(order_item__order=order).exclude(
        status=TicketStatus.REFUNDED
    )
    target = OrderStatus.PARTIALLY_REFUNDED if still_live.exists() else OrderStatus.REFUNDED
    ORDER_MACHINE.apply(order, target, actor=actor, ticket_ids=wanted, amount_cents=amount)
    return refund
