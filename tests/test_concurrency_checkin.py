import pytest
from django.db import transaction

from apps.orders.checkin import check_in
from apps.orders.models import Ticket
from apps.payments.refunds import RefundRejected, refund_tickets
from tests.concurrency import race
from tests.factories import TicketTypeFactory
from tests.helpers import make_paid_order

pytestmark = [pytest.mark.concurrency, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def ticket():
    tt = TicketTypeFactory(capacity=10)
    order = make_paid_order([(tt, 1)])
    return Ticket.objects.get(order_item__order=order)


def test_two_simultaneous_scans_admit_exactly_one(ticket):
    outcomes = race(lambda: check_in(ticket.event_id, ticket.code, None).code, [()] * 2)
    assert sorted(o.value for o in outcomes) == ["admitted", "already_checked_in"]


def test_ten_simultaneous_scans_admit_exactly_one(ticket):
    outcomes = race(lambda: check_in(ticket.event_id, ticket.code, None).code, [()] * 10)
    results = [o.value for o in outcomes]
    assert results.count("admitted") == 1
    assert results.count("already_checked_in") == 9


def test_refund_racing_checkin_never_leaves_a_refunded_ticket_admitted(ticket):
    order_id = ticket.order_item.order_id
    actor = ticket.event.organizer.owner

    def refund():
        with transaction.atomic():
            refund_tickets(order_id, [ticket.pk], actor)
        return "refunded"

    def scan():
        return check_in(ticket.event_id, ticket.code, None).code

    refund_out, scan_out = race(lambda f: f(), [(refund,), (scan,)])
    ticket.refresh_from_db()
    if scan_out.value == "admitted":
        # Check-in won: refund must have been refused, ticket stays checked in.
        assert isinstance(refund_out.error, RefundRejected)
        assert ticket.status == "checked_in"
    else:
        # Refund won: the scan must have been rejected.
        assert refund_out.value == "refunded" and scan_out.value == "ticket_refunded"
        assert ticket.status == "refunded"
