import uuid

import pytest
from rest_framework.test import APIClient

from apps.catalog.models import TicketType
from apps.core.models import IdempotencyKey
from apps.orders.models import Order
from tests.factories import TicketTypeFactory, UserFactory

pytestmark = pytest.mark.django_db


def _post(c, body, key=None):
    return c.post("/api/orders/", body, format="json", HTTP_IDEMPOTENCY_KEY=key or uuid.uuid4().hex)


def _body(tt, qty=2):
    return {"items": [{"ticket_type": tt.id, "quantity": qty}]}


def test_requires_idempotency_key(client_for):
    tt = TicketTypeFactory()
    r = client_for(UserFactory()).post("/api/orders/", _body(tt), format="json")
    assert r.status_code == 400
    assert r.json()["code"] == "idempotency_key_required"
    assert not Order.objects.exists()


def test_create_order_holds_tickets(client_for):
    tt = TicketTypeFactory(capacity=10, price_cents=1500)
    r = _post(client_for(UserFactory()), _body(tt, 3))
    assert r.status_code == 201, r.json()
    data = r.json()
    assert data["status"] == "pending" and data["total_cents"] == 4500
    assert data["items"] == [{"ticket_type": tt.id, "quantity": 3, "unit_price_cents": 1500}]
    tt.refresh_from_db()
    assert tt.reserved_count == 3


def test_retry_same_key_same_body_replays_original(client_for):
    tt = TicketTypeFactory(capacity=10)
    c = client_for(UserFactory())
    first = _post(c, _body(tt), key="k1")
    second = _post(c, _body(tt), key="k1")
    assert second.status_code == 201
    assert second.json() == first.json()
    assert second["Idempotent-Replayed"] == "true"
    assert Order.objects.count() == 1
    tt.refresh_from_db()
    assert tt.reserved_count == 2


def test_same_key_different_body_is_422(client_for):
    tt = TicketTypeFactory(capacity=10)
    c = client_for(UserFactory())
    _post(c, _body(tt, 1), key="k1")
    r = _post(c, _body(tt, 2), key="k1")
    assert r.status_code == 422
    assert r.json()["code"] == "idempotency_key_reused"
    assert Order.objects.count() == 1


def test_keys_are_scoped_per_user(client_for):
    tt = TicketTypeFactory(capacity=10)
    a, b = client_for(UserFactory()), client_for(UserFactory())
    assert _post(a, _body(tt), key="same").status_code == 201
    assert _post(b, _body(tt), key="same").status_code == 201
    assert Order.objects.count() == 2


def test_sold_out_is_409_and_replayed_consistently(client_for):
    tt = TicketTypeFactory(capacity=1)
    c = client_for(UserFactory())
    r = _post(c, _body(tt, 2), key="k")
    assert r.status_code == 409
    assert r.json()["code"] == "sold_out" and r.json()["available"] == 1
    TicketType.objects.filter(pk=tt.pk).update(capacity=10)
    replay = _post(c, _body(tt, 2), key="k")
    assert replay.status_code == 409  # a stored outcome, not re-executed
    assert _post(c, _body(tt, 2), key="new-key").status_code == 201


@pytest.mark.parametrize(
    "body,code",
    [
        ({"items": []}, None),
        ({"items": [{"ticket_type": 999999, "quantity": 1}]}, "unknown_ticket_type"),
    ],
)
def test_validation_errors_are_400(client_for, body, code):
    r = _post(client_for(UserFactory()), body)
    assert r.status_code == 400
    if code:
        assert r.json()["code"] == code


def test_server_error_is_not_stored_so_retry_works(client_for, monkeypatch):
    from apps.orders import services

    tt = TicketTypeFactory(capacity=10)
    user = UserFactory()
    c = client_for(user)
    c.raise_request_exception = False
    real = services.place_order
    monkeypatch.setattr(services, "place_order", lambda *a: (_ for _ in ()).throw(RuntimeError))
    assert _post(c, _body(tt), key="k").status_code == 500
    assert not IdempotencyKey.objects.exists()
    tt.refresh_from_db()
    assert tt.reserved_count == 0
    monkeypatch.setattr(services, "place_order", real)
    assert _post(c, _body(tt), key="k").status_code == 201


def test_buyer_sees_only_own_orders(client_for):
    tt = TicketTypeFactory(capacity=10)
    alice, bob = client_for(UserFactory()), client_for(UserFactory())
    order_id = _post(alice, _body(tt)).json()["id"]
    assert bob.get(f"/api/orders/{order_id}/").status_code == 404
    assert bob.post(f"/api/orders/{order_id}/cancel/").status_code == 404
    assert bob.get(f"/api/orders/{order_id}/tickets/").status_code == 404
    assert bob.get("/api/orders/").json()["results"] == []
    assert [o["id"] for o in alice.get("/api/orders/").json()["results"]] == [order_id]
    assert APIClient().get("/api/orders/").status_code == 401


def test_cancel_releases_hold(client_for):
    tt = TicketTypeFactory(capacity=10)
    c = client_for(UserFactory())
    order_id = _post(c, _body(tt, 4)).json()["id"]
    r = c.post(f"/api/orders/{order_id}/cancel/")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert c.post(f"/api/orders/{order_id}/cancel/").status_code == 409
    tt.refresh_from_db()
    assert tt.reserved_count == 0


def test_metrics_record_checkout(client_for, client):
    tt = TicketTypeFactory(capacity=1)
    c = client_for(UserFactory())
    _post(c, _body(tt, 1))
    _post(c, _body(tt, 1))  # sold out
    text = client.get("/metrics").content.decode()
    assert 'checkout_latency_seconds_count{outcome="created"}' in text
    assert 'checkout_failures_total{reason="sold_out"}' in text
