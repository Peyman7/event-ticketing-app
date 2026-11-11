from datetime import timedelta

import pytest
from django.utils import timezone

from apps.catalog.models import Event
from tests.factories import EventFactory, OrganizerFactory, TicketTypeFactory, UserFactory

pytestmark = pytest.mark.django_db


def test_register_then_obtain_token(api_client):
    r = api_client.post(
        "/api/auth/register/",
        {"username": "ann", "email": "ann@example.com", "password": "long-enough-pw"},
    )
    assert r.status_code == 201 and r.json()["token"]
    r2 = api_client.post("/api/auth/token/", {"username": "ann", "password": "long-enough-pw"})
    assert r2.json()["token"] == r.json()["token"]


def test_protected_endpoints_require_auth(api_client):
    assert api_client.get("/api/organizer/events/").status_code == 401
    assert api_client.get("/api/organizers/").status_code == 401


def test_public_events_list_only_published(api_client):
    pub = TicketTypeFactory(capacity=10, reserved_count=3).event
    EventFactory(is_published=False)
    r = api_client.get("/api/events/")
    assert r.status_code == 200
    results = r.json()["results"]
    assert [e["id"] for e in results] == [pub.id]
    assert results[0]["ticket_types"][0]["available"] == 7
    assert "reserved_count" not in results[0]["ticket_types"][0]


def test_organizer_creates_event_and_ticket_type(client_for):
    org = OrganizerFactory()
    c = client_for(org.owner)
    r = c.post(
        "/api/organizer/events/",
        {
            "organizer": org.id,
            "title": "Gig",
            "starts_at": timezone.now() + timedelta(days=9),
            "currency": "EUR",
        },
        format="json",
    )
    assert r.status_code == 201, r.json()
    ev = r.json()["id"]
    now = timezone.now()
    r = c.post(
        f"/api/organizer/events/{ev}/ticket-types/",
        {
            "name": "GA",
            "price_cents": 2500,
            "capacity": 100,
            "sale_starts_at": now,
            "sale_ends_at": now + timedelta(days=1),
        },
        format="json",
    )
    assert r.status_code == 201, r.json()
    assert r.json()["reserved_count"] == 0


def test_cannot_create_event_for_someone_elses_organizer(client_for):
    mine, theirs = OrganizerFactory(), OrganizerFactory()
    r = client_for(mine.owner).post(
        "/api/organizer/events/",
        {"organizer": theirs.id, "title": "x", "starts_at": timezone.now(), "currency": "USD"},
        format="json",
    )
    assert r.status_code == 400
    assert not Event.objects.filter(organizer=theirs).exists()


def test_organizer_cannot_see_or_edit_other_organizers_events(client_for):
    tt = TicketTypeFactory()
    other = client_for(OrganizerFactory().owner)
    ev = tt.event.id
    assert other.get(f"/api/organizer/events/{ev}/").status_code == 404
    assert other.patch(f"/api/organizer/events/{ev}/", {"title": "pwned"}).status_code == 404
    assert other.get(f"/api/organizer/events/{ev}/ticket-types/").json()["results"] == []
    r = other.patch(f"/api/organizer/events/{ev}/ticket-types/{tt.id}/", {"capacity": 1})
    assert r.status_code == 404
    r = other.post(f"/api/organizer/events/{ev}/ticket-types/", {"name": "x"})
    assert r.status_code in (400, 404)
    assert other.get("/api/organizer/events/").json()["results"] == []


def test_buyer_without_organizer_sees_nothing(client_for):
    EventFactory()
    assert client_for(UserFactory()).get("/api/organizer/events/").json()["results"] == []


def test_capacity_cannot_drop_below_reserved(client_for):
    tt = TicketTypeFactory(capacity=10, reserved_count=6)
    c = client_for(tt.event.organizer.owner)
    url = f"/api/organizer/events/{tt.event.id}/ticket-types/{tt.id}/"
    assert c.patch(url, {"capacity": 5}).status_code == 400
    assert c.patch(url, {"capacity": 6}).status_code == 200


def test_event_currency_and_organizer_are_immutable(client_for):
    ev = EventFactory(currency="USD")
    c = client_for(ev.organizer.owner)
    c.patch(f"/api/organizer/events/{ev.id}/", {"currency": "EUR"}, format="json")
    ev.refresh_from_db()
    assert ev.currency == "USD"
