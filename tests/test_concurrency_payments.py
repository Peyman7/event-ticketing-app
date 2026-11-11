import uuid

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.orders.models import Order
from apps.payments.models import Payment
from tests.concurrency import race
from tests.factories import TicketTypeFactory, UserFactory

pytestmark = [pytest.mark.concurrency, pytest.mark.django_db(transaction=True)]


def test_concurrent_pay_attempts_with_different_keys_create_one_payment():
    """A double-clicking client that generates a fresh key per click must still not
    start two charges: the order row lock + partial unique index allow one."""
    tt = TicketTypeFactory(capacity=10)
    user = UserFactory()
    token = Token.objects.create(user=user).key
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token}")
    order_id = c.post(
        "/api/orders/",
        {"items": [{"ticket_type": tt.id, "quantity": 1}]},
        format="json",
        HTTP_IDEMPOTENCY_KEY="o",
    ).json()["id"]

    def pay():
        cl = APIClient()
        cl.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        r = cl.post(
            f"/api/orders/{order_id}/pay/",
            {"payment_method": "pm_ok"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
        )
        return r.status_code, r.json().get("code")

    outcomes = race(pay, [()] * 10)
    assert all(o.error is None for o in outcomes), [o.error for o in outcomes]
    results = sorted(o.value for o in outcomes)
    assert results.count((202, None)) == 1
    assert results.count((409, "payment_in_flight")) == 9
    assert Payment.objects.filter(order_id=order_id).count() == 1
    assert Order.objects.get(pk=order_id).status == "pending"
