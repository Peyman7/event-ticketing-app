from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from apps.orders.checkin import check_in
from apps.orders.models import Ticket
from apps.orders.pricing import LineRequest
from apps.orders.services import place_order
from apps.payments.refunds import refund_tickets
from apps.reporting import queries
from tests.factories import OrganizerFactory, TicketTypeFactory, UserFactory
from tests.helpers import make_paid_order

pytestmark = pytest.mark.django_db


@pytest.fixture
def scenario():
    """GA @3000 (cap 10), VIP @9000 (cap 5).
    A: paid 2 GA + 1 VIP, then 1 GA refunded.  B: paid 1 GA.  C: pending 2 GA (held).
    Check-ins: A's VIP, B's GA."""
    ga = TicketTypeFactory(name="GA", price_cents=3000, capacity=10)
    vip = TicketTypeFactory(event=ga.event, name="VIP", price_cents=9000, capacity=5)
    a = make_paid_order([(ga, 2), (vip, 1)])
    b = make_paid_order([(ga, 1)])
    with transaction.atomic():
        place_order(UserFactory(), [LineRequest(ga.id, 2)])
    a_ga = Ticket.objects.filter(order_item__order=a, ticket_type=ga).first()
    with transaction.atomic():
        refund_tickets(a.pk, [a_ga.pk], ga.event.organizer.owner)
    for t in [
        Ticket.objects.get(order_item__order=a, ticket_type=vip),
        Ticket.objects.get(order_item__order=b),
    ]:
        assert check_in(ga.event_id, t.code, None).admitted
    return ga.event, ga, vip


def test_sales_by_ticket_type_numbers(scenario):
    event, ga, vip = scenario
    rows = {r["name"]: r for r in queries.sales_by_ticket_type(event.pk)}
    assert rows["GA"] == {
        "id": ga.id,
        "name": "GA",
        "price_cents": 3000,
        "capacity": 10,
        "held": 2,
        "sold": 2,
        "checked_in": 1,
        "refunded": 1,
        "gross_cents": 9000,
        "refunded_cents": 3000,
        "net_cents": 6000,
    }
    assert rows["VIP"] == {
        "id": vip.id,
        "name": "VIP",
        "price_cents": 9000,
        "capacity": 5,
        "held": 0,
        "sold": 1,
        "checked_in": 1,
        "refunded": 0,
        "gross_cents": 9000,
        "refunded_cents": 0,
        "net_cents": 9000,
    }


def test_event_report_totals_and_cash_view(scenario):
    event, _, _ = scenario
    rep = queries.event_report(event.pk)
    t = rep["totals"]
    assert (t["sold"], t["checked_in"], t["held"], t["net_cents"]) == (3, 2, 2, 15000)
    assert t["attendance_rate"] == round(2 / 3, 4)
    assert rep["payments"] == {
        "collected_cents": 18000,
        "refunded_cents": 3000,
        "net_cents": 15000,
        "payments": 2,
    }


def test_report_query_count_is_constant(scenario, django_assert_num_queries):
    event, _, _ = scenario
    for i in range(5):
        TicketTypeFactory(event=event, name=f"extra{i}")
    with django_assert_num_queries(2):  # ticket types + payments, regardless of row count
        queries.event_report(event.pk)


def test_timeline_fills_empty_buckets(scenario):
    event, _, _ = scenario
    series = queries.sales_timeline(event.pk, "hour")
    assert len(series) >= 24  # sale window opened a day ago: one bucket per hour
    assert all(s["bucket"].minute == 0 for s in series)
    assert sum(s["tickets"] for s in series) == 4  # sales at sale time; refunds not netted
    assert sum(s["gross_cents"] for s in series) == 18000
    assert sum(1 for s in series if s["tickets"]) == 1
    assert series[-1]["bucket"] <= timezone.now() < series[-1]["bucket"] + timedelta(hours=1)


def test_report_endpoints_permissions(scenario, client_for):
    event, _, _ = scenario
    owner = client_for(event.organizer.owner)
    r = owner.get(f"/api/organizer/events/{event.pk}/report/")
    assert r.status_code == 200 and r.json()["totals"]["sold"] == 3
    r = owner.get(f"/api/organizer/events/{event.pk}/report/timeline/?bucket=day")
    assert r.status_code == 200 and r.json()["bucket"] == "day"
    assert (
        owner.get(f"/api/organizer/events/{event.pk}/report/timeline/?bucket=x").status_code == 400
    )
    stranger = client_for(OrganizerFactory().owner)
    assert stranger.get(f"/api/organizer/events/{event.pk}/report/").status_code == 404
    assert stranger.get(f"/api/organizer/events/{event.pk}/report/timeline/").status_code == 404
