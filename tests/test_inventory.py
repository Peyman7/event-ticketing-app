from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from apps.catalog.models import TicketType
from apps.orders.inventory import SoldOut, release_order_hold, reserve
from apps.orders.models import OrderStatus
from apps.orders.pricing import LineRequest as L
from apps.orders.pricing import PricingError
from apps.orders.reconcile import inventory_mismatches
from tests.factories import TicketTypeFactory, UserFactory

pytestmark = pytest.mark.django_db


def _reserve(buyer, *lines):
    with transaction.atomic():
        return reserve(buyer, [L(*line) for line in lines])


def test_reserve_holds_capacity_and_snapshots_price():
    tt = TicketTypeFactory(capacity=10, price_cents=1234)
    order = _reserve(UserFactory(), (tt.id, 3))
    tt.refresh_from_db()
    assert tt.reserved_count == 3
    assert order.status == OrderStatus.PENDING
    assert order.total_cents == 3 * 1234
    assert order.currency == "USD"
    assert timezone.now() + timedelta(minutes=9) < order.expires_at
    assert order.items.get().unit_price_cents == 1234
    assert inventory_mismatches() == []


def test_sold_out_leaves_counter_untouched():
    tt = TicketTypeFactory(capacity=2)
    with pytest.raises(SoldOut) as exc:
        _reserve(UserFactory(), (tt.id, 3))
    assert exc.value.available == 2
    tt.refresh_from_db()
    assert tt.reserved_count == 0


def test_multi_line_order_is_all_or_nothing():
    a = TicketTypeFactory(capacity=5)
    b = TicketTypeFactory(event=a.event, capacity=1)
    with pytest.raises(SoldOut):
        _reserve(UserFactory(), (a.id, 2), (b.id, 2))
    assert list(TicketType.objects.values_list("reserved_count", flat=True)) == [0, 0]


def test_unpublished_event_rejected():
    tt = TicketTypeFactory(event__is_published=False)
    with pytest.raises(PricingError):
        _reserve(UserFactory(), (tt.id, 1))


@pytest.mark.django_db(transaction=True)
def test_reserve_requires_transaction():
    tt = TicketTypeFactory()
    with pytest.raises(RuntimeError):
        reserve(UserFactory(), [L(tt.id, 1)])


def test_release_returns_capacity():
    tt = TicketTypeFactory(capacity=5)
    order = _reserve(UserFactory(), (tt.id, 4))
    with transaction.atomic():
        release_order_hold(order)
        order.status = OrderStatus.CANCELLED
        order.save()
    tt.refresh_from_db()
    assert tt.reserved_count == 0
    assert inventory_mismatches() == []
