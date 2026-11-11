"""Inventory: reserve (hold) and release ticket capacity.

Invariant:  TicketType.reserved_count == held (pending orders) + sold (valid/checked-in tickets)
and         0 <= reserved_count <= capacity   (also a DB CHECK constraint)

Concurrency strategy: pessimistic row locks (SELECT ... FOR UPDATE) on the TicketType
rows, always taken in ascending id order so two multi-type carts cannot deadlock.
Lock hierarchy across the codebase: Order -> Payment -> Ticket -> TicketType
(see README "Lock ordering" for the deadlock this ordering avoids).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.catalog.models import Event, TicketType
from apps.core.audit import audit
from apps.core.context import bind_order

from .models import Order, OrderItem
from .pricing import LineRequest, PricingError, normalize_lines, quote

logger = logging.getLogger(__name__)


class SoldOut(Exception):
    code = "sold_out"

    def __init__(self, ticket_type_id: int, requested: int, available: int):
        self.ticket_type_id, self.requested, self.available = ticket_type_id, requested, available
        super().__init__(
            f"Ticket type {ticket_type_id}: requested {requested}, available {available}."
        )


def reserve(buyer, lines: Iterable[LineRequest], *, now: datetime | None = None) -> Order:
    """Atomically hold tickets and create a pending order. Raises SoldOut / PricingError.

    Must run inside a transaction (the caller's, so the idempotency record and the
    expiry job commit together with the hold).
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("reserve() must be called inside transaction.atomic()")
    now = now or timezone.now()
    lines = normalize_lines(lines)

    # ORDER BY id + FOR UPDATE: Postgres locks rows in the sorted order (LockRows sits
    # above Sort in the plan), which is our global lock order.
    locked = (
        TicketType.objects.select_for_update()
        .filter(id__in=[line.ticket_type_id for line in lines])
        .order_by("id")
    )
    types = {tt.id: tt for tt in locked}
    q = quote(lines, types, now=now)

    event = Event.objects.get(pk=q.event_id)
    if not event.is_published:
        raise PricingError("not_on_sale", "Event is not published.")

    for line in q.lines:
        tt = types[line.ticket_type_id]
        if tt.reserved_count + line.quantity > tt.capacity:
            raise SoldOut(tt.id, line.quantity, tt.available)

    for line in q.lines:
        TicketType.objects.filter(pk=line.ticket_type_id).update(
            reserved_count=F("reserved_count") + line.quantity
        )

    order = Order.objects.create(
        buyer=buyer,
        event=event,
        total_cents=q.total_cents,
        currency=event.currency,
        expires_at=now + timedelta(minutes=settings.HOLD_MINUTES),
    )
    OrderItem.objects.bulk_create(
        OrderItem(
            order=order,
            ticket_type_id=line.ticket_type_id,
            quantity=line.quantity,
            unit_price_cents=line.unit_price_cents,
        )
        for line in q.lines
    )
    bind_order(order.pk)
    audit(
        order, "reserved", actor=buyer, lines=[[li.ticket_type_id, li.quantity] for li in q.lines]
    )
    logger.info("tickets reserved", extra={"total_cents": order.total_cents})
    return order


def release(counts: dict[int, int]) -> None:
    """Give capacity back: {ticket_type_id: quantity}. Caller holds the owning row's lock.

    Updated in ascending id order, same as reserve(), so the implicit row locks taken by
    UPDATE follow the global lock order.
    """
    for tt_id in sorted(counts):
        if counts[tt_id]:
            TicketType.objects.filter(pk=tt_id).update(
                reserved_count=F("reserved_count") - counts[tt_id]
            )


def release_order_hold(order: Order) -> None:
    """Release every ticket held by a pending order (expiry / cancellation)."""
    release({item.ticket_type_id: item.quantity for item in order.items.all()})
