"""Order lifecycle services. Views and jobs stay thin; locking + transitions live here."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from apps.core.context import bind_order, get_request_id

from .inventory import release_order_hold, reserve
from .models import ORDER_MACHINE, Order, OrderStatus
from .pricing import LineRequest

logger = logging.getLogger(__name__)

SWEEP_BATCH = 500


def place_order(buyer, lines: Iterable[LineRequest]) -> Order:
    """Hold tickets and schedule the hold's expiry, in the caller's transaction.

    The expiry job row is inserted on the same connection/transaction as the hold, so
    either both exist or neither does (transactional outbox via Procrastinate).
    """
    from .tasks import expire_order_task

    order = reserve(buyer, lines)
    expire_order_task.configure(schedule_at=order.expires_at).defer(
        order_id=order.pk, request_id=get_request_id()
    )
    return order


def cancel_order(order_id: int, actor) -> Order:
    """Buyer gives up a pending hold early."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        bind_order(order.pk)
        ORDER_MACHINE.check(order.status, OrderStatus.CANCELLED)
        release_order_hold(order)
        ORDER_MACHINE.apply(order, OrderStatus.CANCELLED, actor=actor, reason="buyer_cancelled")
        return order


def expire_order(order_id: int, *, now: datetime | None = None, skip_locked: bool = False) -> bool:
    """Release a pending order's hold if it is past due. Idempotent; returns True if expired.

    skip_locked=True is used by the sweeper so it never queues behind a checkout/webhook
    that currently holds the order row; the next sweep will retry it.
    """
    now = now or timezone.now()
    with transaction.atomic():
        order = (
            Order.objects.select_for_update(skip_locked=skip_locked)
            .filter(pk=order_id, status=OrderStatus.PENDING)
            .first()
        )
        if order is None or order.expires_at > now:
            return False
        bind_order(order.pk)
        release_order_hold(order)
        ORDER_MACHINE.apply(order, OrderStatus.EXPIRED, reason="hold_expired")
        logger.info("hold expired")
        return True


def sweep_expired_holds(*, now: datetime | None = None) -> int:
    """Safety net for lost/late expiry jobs. Uses the partial index on pending orders."""
    now = now or timezone.now()
    ids = list(
        Order.objects.filter(status=OrderStatus.PENDING, expires_at__lte=now)
        .order_by("expires_at")
        .values_list("id", flat=True)[:SWEEP_BATCH]
    )
    return sum(expire_order(oid, now=now, skip_locked=True) for oid in ids)
