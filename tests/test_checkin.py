import pytest

from apps.orders.models import Ticket
from tests.factories import OrganizerFactory, TicketTypeFactory, UserFactory
from tests.helpers import make_paid_order

pytestmark = pytest.mark.django_db


@pytest.fixture
def paid(client_for):
    tt = TicketTypeFactory(capacity=10)
    order = make_paid_order([(tt, 2)])
    t1, t2 = Ticket.objects.filter(order_item__order=order).order_by("id")
    return tt.event, t1, t2, client_for(tt.event.organizer.owner)


def _scan(c, event, code):
    return c.post("/api/checkin/", {"event": event.pk, "code": code}, format="json")


def test_first_scan_admits_second_rejects(paid):
    event, t1, _, org = paid
    r = _scan(org, event, t1.code)
    assert r.status_code == 200 and r.json()["result"] == "admitted"
    t1.refresh_from_db()
    assert t1.status == "checked_in" and t1.checked_in_at
    again = _scan(org, event, t1.code)
    assert again.status_code == 409 and again.json()["result"] == "already_checked_in"
    assert again.json()["checked_in_at"]


def test_unknown_code_and_wrong_event_are_404(paid):
    event, t1, _, org = paid
    assert _scan(org, event, "nope").json()["result"] == "unknown_ticket"
    other_event = TicketTypeFactory(event__organizer=event.organizer).event
    r = _scan(org, other_event, t1.code)  # valid ticket, but for a different show
    assert r.status_code == 404
    t1.refresh_from_db()
    assert t1.status == "valid"


def test_refunded_ticket_rejected(paid):
    event, t1, _, org = paid
    Ticket.objects.filter(pk=t1.pk).update(status="refunded")
    r = _scan(org, event, t1.code)
    assert r.status_code == 409 and r.json()["result"] == "ticket_refunded"


def test_only_events_organizer_can_scan(paid, client_for):
    event, t1, _, _ = paid
    assert _scan(client_for(OrganizerFactory().owner), event, t1.code).status_code == 404
    assert _scan(client_for(UserFactory()), event, t1.code).status_code == 404
    t1.refresh_from_db()
    assert t1.status == "valid"
