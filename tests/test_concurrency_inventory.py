"""Real multi-threaded races against Postgres. No mocks, no SQLite."""

import random

import pytest
from django.db import transaction

from apps.orders.inventory import SoldOut, reserve
from apps.orders.models import Order
from apps.orders.pricing import LineRequest as L
from apps.orders.reconcile import inventory_mismatches
from tests.concurrency import race
from tests.factories import TicketTypeFactory, UserFactory

pytestmark = [pytest.mark.concurrency, pytest.mark.django_db(transaction=True)]


def _buy(buyer, lines):
    with transaction.atomic():
        return reserve(buyer, lines).pk


def test_50_buyers_race_for_last_5_tickets_exactly_5_sell():
    # 15 seats, 10 already held by an earlier order -> 5 left. Using a real order (not a
    # bare counter bump) keeps the reconciliation invariant meaningful.
    tt = TicketTypeFactory(capacity=15, max_per_order=10)
    _buy(UserFactory(), [L(tt.id, 10)])

    buyers = [UserFactory() for _ in range(50)]
    outcomes = race(_buy, [(b, [L(tt.id, 1)]) for b in buyers])

    winners = [o for o in outcomes if o.error is None]
    losers = [o for o in outcomes if o.error is not None]
    assert len(winners) == 5
    assert all(isinstance(o.error, SoldOut) for o in losers), [o.error for o in losers]
    tt.refresh_from_db()
    assert tt.reserved_count == tt.capacity == 15
    assert Order.objects.filter(buyer__in=buyers).count() == 5
    assert inventory_mismatches() == []


def test_random_quantities_never_oversell():
    tt = TicketTypeFactory(capacity=37, max_per_order=4)
    rng = random.Random(42)
    buyers = [(UserFactory(), [L(tt.id, rng.randint(1, 4))]) for _ in range(50)]
    outcomes = race(_buy, buyers)

    assert all(o.error is None or isinstance(o.error, SoldOut) for o in outcomes)
    tt.refresh_from_db()
    sold = sum(
        lines[0].quantity for (_, lines), o in zip(buyers, outcomes, strict=True) if not o.error
    )
    assert tt.reserved_count == sold <= 37
    assert inventory_mismatches() == []


def test_opposite_cart_order_does_not_deadlock():
    """Half the buyers send [A, B], half send [B, A]. Sorted locking means no deadlock
    errors, only clean SoldOut."""
    a = TicketTypeFactory(capacity=20)
    b = TicketTypeFactory(event=a.event, capacity=20)
    args = []
    for i in range(40):
        lines = [L(a.id, 1), L(b.id, 1)]
        args.append((UserFactory(), lines if i % 2 else lines[::-1]))
    outcomes = race(_buy, args)

    errors = [o.error for o in outcomes if o.error]
    assert all(isinstance(e, SoldOut) for e in errors), errors
    assert sum(o.error is None for o in outcomes) == 20
    assert inventory_mismatches() == []
