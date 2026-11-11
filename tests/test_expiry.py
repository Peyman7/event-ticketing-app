from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.core.models import AuditLog
from apps.orders import services
from apps.orders.models import OrderStatus
from apps.orders.pricing import LineRequest as L
from apps.orders.reconcile import inventory_mismatches
from tests.concurrency import race
from tests.factories import TicketTypeFactory, UserFactory


def _place(tt, qty=2, buyer=None):
    with transaction.atomic():
        return services.place_order(buyer or UserFactory(), [L(tt.id, qty)])


class Boom(Exception):
    pass


@pytest.mark.django_db(transaction=True)
def test_expiry_job_commits_and_rolls_back_with_the_hold():
    """The transactional-outbox property: job row and hold share one transaction."""
    tt = TicketTypeFactory(capacity=10)
    with pytest.raises(Boom), transaction.atomic():
        services.place_order(UserFactory(), [L(tt.id, 1)])
        assert ProcrastinateJob.objects.count() == 1  # visible inside the txn...
        raise Boom
    assert ProcrastinateJob.objects.count() == 0  # ...gone with the rollback
    tt.refresh_from_db()
    assert tt.reserved_count == 0

    order = _place(tt)
    job = ProcrastinateJob.objects.get()
    assert job.task_name == "orders.expire_order"
    assert job.args["order_id"] == order.pk
    assert job.scheduled_at == order.expires_at


@pytest.mark.django_db
def test_expire_before_deadline_is_noop():
    tt = TicketTypeFactory(capacity=10)
    order = _place(tt)
    assert services.expire_order(order.pk) is False
    order.refresh_from_db()
    assert order.status == OrderStatus.PENDING


@pytest.mark.django_db
def test_expire_after_deadline_releases_once():
    tt = TicketTypeFactory(capacity=10)
    order = _place(tt, qty=3)
    later = order.expires_at + timedelta(seconds=1)
    assert services.expire_order(order.pk, now=later) is True
    assert services.expire_order(order.pk, now=later) is False  # idempotent
    order.refresh_from_db()
    tt.refresh_from_db()
    assert order.status == OrderStatus.EXPIRED
    assert tt.reserved_count == 0
    assert AuditLog.objects.filter(to_state="expired").count() == 1
    assert inventory_mismatches() == []


@pytest.mark.django_db
def test_task_wrapper_runs_service():
    from apps.orders.tasks import expire_order_task

    order = _place(TicketTypeFactory())
    order.__class__.objects.filter(pk=order.pk).update(expires_at=timezone.now())
    assert expire_order_task(order_id=order.pk, request_id="rid-1") is True


@pytest.mark.django_db
def test_sweeper_expires_only_past_due_pending_orders():
    tt = TicketTypeFactory(capacity=50)
    due = [_place(tt, 1) for _ in range(3)]
    fresh = _place(tt, 1)
    cancelled = _place(tt, 1)
    services.cancel_order(cancelled.pk, actor=cancelled.buyer)

    now = due[0].expires_at + timedelta(minutes=1)
    fresh.__class__.objects.filter(pk=fresh.pk).update(expires_at=now + timedelta(minutes=5))
    assert services.sweep_expired_holds(now=now) == 3
    statuses = {o.pk: o.__class__.objects.get(pk=o.pk).status for o in [*due, fresh, cancelled]}
    assert [statuses[o.pk] for o in due] == ["expired"] * 3
    assert statuses[fresh.pk] == "pending"
    assert statuses[cancelled.pk] == "cancelled"
    tt.refresh_from_db()
    assert tt.reserved_count == 1
    assert inventory_mismatches() == []


@pytest.mark.django_db
def test_cancel_rejected_once_expired():
    from apps.core.state_machine import InvalidTransition

    order = _place(TicketTypeFactory())
    services.expire_order(order.pk, now=order.expires_at + timedelta(seconds=1))
    with pytest.raises(InvalidTransition):
        services.cancel_order(order.pk, actor=order.buyer)


@pytest.mark.concurrency
@pytest.mark.django_db(transaction=True)
def test_expiry_job_and_sweeper_racing_release_exactly_once():
    tt = TicketTypeFactory(capacity=10)
    orders = [_place(tt, 2) for _ in range(4)]
    now = orders[0].expires_at + timedelta(seconds=5)
    args = [(o.pk,) for o in orders for _ in range(5)]  # 5 concurrent expirers per order

    def expire(order_id):
        return services.expire_order(order_id, now=now)

    outcomes = race(lambda oid: expire(oid), args)
    sweeper = race(lambda: services.sweep_expired_holds(now=now), [()] * 3)

    assert all(o.error is None for o in outcomes + sweeper)
    assert sum(o.value for o in outcomes) + sum(o.value for o in sweeper) == 4
    tt.refresh_from_db()
    assert tt.reserved_count == 0  # would go negative (CHECK violation) on double release
    assert inventory_mismatches() == []
