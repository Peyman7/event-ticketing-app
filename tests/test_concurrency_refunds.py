import pytest
from django.db import transaction

from apps.orders.models import Ticket
from apps.orders.reconcile import inventory_mismatches
from apps.payments.models import Payment, Refund
from apps.payments.refunds import RefundRejected, refund_tickets
from tests.concurrency import race
from tests.factories import TicketTypeFactory
from tests.helpers import make_paid_order

pytestmark = [pytest.mark.concurrency, pytest.mark.django_db(transaction=True)]


def _refund(order_id, ids, actor):
    with transaction.atomic():
        return refund_tickets(order_id, ids, actor)


def test_concurrent_refunds_of_the_same_ticket_refund_once():
    tt = TicketTypeFactory(capacity=10, price_cents=2000)
    order = make_paid_order([(tt, 3)])
    ticket = Ticket.objects.filter(order_item__order=order).first()
    actor = tt.event.organizer.owner

    outcomes = race(_refund, [(order.pk, [ticket.pk], actor)] * 8)

    ok = [o for o in outcomes if o.error is None]
    assert len(ok) == 1
    assert all(isinstance(o.error, RefundRejected) for o in outcomes if o.error)
    assert Payment.objects.get().refunded_cents == 2000
    assert Refund.objects.count() == 1
    assert inventory_mismatches() == []


def test_concurrent_refunds_of_different_tickets_all_apply_and_sum_correctly():
    tt = TicketTypeFactory(capacity=10, price_cents=2000)
    order = make_paid_order([(tt, 6)])
    ids = list(Ticket.objects.filter(order_item__order=order).values_list("pk", flat=True))
    actor = tt.event.organizer.owner

    outcomes = race(_refund, [(order.pk, [i], actor) for i in ids])

    assert all(o.error is None for o in outcomes), [o.error for o in outcomes]
    p = Payment.objects.get()
    assert p.refunded_cents == p.amount_cents == 12000
    order.refresh_from_db()
    assert order.status == "refunded"
    tt.refresh_from_db()
    assert tt.reserved_count == 0
    assert inventory_mismatches() == []
