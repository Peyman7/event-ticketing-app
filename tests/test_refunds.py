import uuid

import pytest
from django.db import transaction

from apps.orders.models import Ticket
from apps.orders.reconcile import inventory_mismatches
from apps.payments.models import Payment, Refund
from tests.factories import OrganizerFactory, TicketTypeFactory, UserFactory
from tests.helpers import make_paid_order
from tests.jobs import drain

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(client_for):
    """Paid order: 2 x GA @ 3000 + 1 x VIP @ 9000 = 15000 cents."""
    ga = TicketTypeFactory(capacity=10, price_cents=3000, name="GA")
    vip = TicketTypeFactory(event=ga.event, capacity=5, price_cents=9000, name="VIP")
    order = make_paid_order([(ga, 2), (vip, 1)])
    tickets = {t.ticket_type_id: [] for t in Ticket.objects.all()}
    for t in Ticket.objects.order_by("id"):
        tickets[t.ticket_type_id].append(t)
    org = client_for(ga.event.organizer.owner)
    return order, ga, vip, tickets, org


def _refund(c, order, ticket_ids, key=None):
    return c.post(
        f"/api/organizer/orders/{order.pk}/refunds/",
        {"ticket_ids": ticket_ids},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key or uuid.uuid4().hex,
    )


def test_partial_refund_invalidates_tickets_and_releases_capacity(setup, client):
    order, ga, _, tickets, org = setup
    r = _refund(org, order, [tickets[ga.id][0].pk])
    assert r.status_code == 201, r.json()
    assert r.json()["refund"]["amount_cents"] == 3000
    assert r.json()["order"]["status"] == "partially_refunded"
    t = Ticket.objects.get(pk=tickets[ga.id][0].pk)
    assert t.status == "refunded"
    ga.refresh_from_db()
    assert ga.reserved_count == 1  # seat back on sale
    assert Payment.objects.get().refunded_cents == 3000
    assert inventory_mismatches() == []

    drain(client)
    assert Refund.objects.get().status == "succeeded"


def test_refunding_everything_moves_order_to_refunded(setup):
    order, ga, vip, tickets, org = setup
    _refund(org, order, [tickets[ga.id][0].pk])
    r = _refund(org, order, [tickets[ga.id][1].pk, tickets[vip.id][0].pk])
    assert r.status_code == 201
    assert r.json()["order"]["status"] == "refunded"
    p = Payment.objects.get()
    assert p.refunded_cents == p.amount_cents == 15000


def test_cannot_refund_same_ticket_twice(setup):
    order, ga, _, tickets, org = setup
    _refund(org, order, [tickets[ga.id][0].pk])
    r = _refund(org, order, [tickets[ga.id][0].pk])
    assert r.status_code == 409 and r.json()["code"] == "already_refunded"
    assert Payment.objects.get().refunded_cents == 3000


def test_cannot_refund_checked_in_ticket(setup):
    order, ga, _, tickets, org = setup
    Ticket.objects.filter(pk=tickets[ga.id][0].pk).update(
        status="checked_in", checked_in_at=order.paid_at
    )
    r = _refund(org, order, [tickets[ga.id][0].pk])
    assert r.status_code == 409 and r.json()["code"] == "ticket_checked_in"


def test_cannot_refund_tickets_from_another_order(setup):
    order, ga, _, _, org = setup
    other = make_paid_order([(ga, 1)])
    foreign = Ticket.objects.get(order_item__order=other)
    r = _refund(org, order, [foreign.pk])
    assert r.status_code == 400 and r.json()["code"] == "unknown_ticket"


def test_over_refund_blocked_even_if_counter_is_tampered(setup):
    """App check + DB CHECK constraint: refunded_cents can never exceed amount."""
    order, ga, _, tickets, org = setup
    Payment.objects.filter(order=order).update(refunded_cents=14000)  # simulate drift
    r = _refund(org, order, [tickets[ga.id][0].pk])
    assert r.status_code == 409 and r.json()["code"] == "over_refund"


def test_refund_requires_paid_order(client_for):
    from apps.orders.pricing import LineRequest
    from apps.orders.services import place_order

    tt = TicketTypeFactory()
    with transaction.atomic():
        order = place_order(UserFactory(), [LineRequest(tt.id, 1)])
    r = _refund(client_for(tt.event.organizer.owner), order, [1])
    assert r.status_code == 409 and r.json()["code"] == "invalid_state"


def test_refund_is_idempotent(setup):
    order, ga, _, tickets, org = setup
    a = _refund(org, order, [tickets[ga.id][0].pk], key="r1")
    b = _refund(org, order, [tickets[ga.id][0].pk], key="r1")
    assert b.status_code == 201 and b.json() == a.json()
    assert Refund.objects.count() == 1


def test_only_the_events_organizer_can_refund_or_view(setup, client_for):
    order, ga, _, tickets, _ = setup
    stranger = client_for(OrganizerFactory().owner)
    assert _refund(stranger, order, [tickets[ga.id][0].pk]).status_code == 404
    assert stranger.get(f"/api/organizer/orders/{order.pk}/").status_code == 404
    buyer = client_for(order.buyer)
    assert _refund(buyer, order, [tickets[ga.id][0].pk]).status_code == 404
    assert not Refund.objects.exists()
