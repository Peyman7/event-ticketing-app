import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import IdempotencyKey
from apps.orders.models import Order
from apps.orders.reconcile import inventory_mismatches
from tests.concurrency import race
from tests.factories import TicketTypeFactory, UserFactory

pytestmark = [pytest.mark.concurrency, pytest.mark.django_db(transaction=True)]


def _post_as(token: str, body: dict, key: str):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token}")
    r = c.post("/api/orders/", body, format="json", HTTP_IDEMPOTENCY_KEY=key)
    return r.status_code, r.json(), r.headers.get("Idempotent-Replayed")


def test_concurrent_duplicates_create_exactly_one_order():
    tt = TicketTypeFactory(capacity=100)
    token = Token.objects.create(user=UserFactory()).key
    body = {"items": [{"ticket_type": tt.id, "quantity": 2}]}

    outcomes = race(_post_as, [(token, body, "dup-key")] * 20)

    assert all(o.error is None for o in outcomes), [o.error for o in outcomes]
    results = [o.value for o in outcomes]
    assert {status for status, _, _ in results} == {201}
    assert len({data["id"] for _, data, _ in results}) == 1
    assert sum(replayed == "true" for _, _, replayed in results) == 19
    assert Order.objects.count() == 1
    assert IdempotencyKey.objects.count() == 1
    tt.refresh_from_db()
    assert tt.reserved_count == 2
    assert inventory_mismatches() == []


def test_concurrent_same_key_different_bodies_one_wins_rest_422_or_replay():
    tt = TicketTypeFactory(capacity=100)
    token = Token.objects.create(user=UserFactory()).key
    args = [
        (token, {"items": [{"ticket_type": tt.id, "quantity": 1 + i % 3}]}, "k") for i in range(15)
    ]

    outcomes = race(_post_as, args)

    statuses = sorted(o.value[0] for o in outcomes)
    assert statuses.count(201) >= 1 and set(statuses) <= {201, 422}
    assert Order.objects.count() == 1
    order = Order.objects.get()
    winners = {o.value[1]["id"] for o in outcomes if o.value[0] == 201}
    assert winners == {order.id}
    assert inventory_mismatches() == []
