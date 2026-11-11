import uuid
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.orders import services as order_services
from apps.orders.models import Order, OrderStatus
from apps.payments import services
from apps.payments.models import FakeProviderObject, Payment, PaymentStatus
from apps.payments.providers.base import ProviderError
from tests.factories import TicketTypeFactory, UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def order(client_for):
    tt = TicketTypeFactory(capacity=10, price_cents=2000)
    buyer = UserFactory()
    c = client_for(buyer)
    r = c.post(
        "/api/orders/",
        {"items": [{"ticket_type": tt.id, "quantity": 2}]},
        format="json",
        HTTP_IDEMPOTENCY_KEY="o1",
    )
    o = Order.objects.get(pk=r.json()["id"])
    o.client = c
    return o


def _pay(order, method="pm_ok", key=None):
    return order.client.post(
        f"/api/orders/{order.pk}/pay/",
        {"payment_method": method},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key or uuid.uuid4().hex,
    )


def test_pay_creates_processing_payment_and_enqueues_submission(order):
    r = _pay(order)
    assert r.status_code == 202, r.json()
    assert r.json()["status"] == "processing" and r.json()["amount_cents"] == 4000
    p = Payment.objects.get()
    assert p.provider_payment_id is None  # not submitted inside the request
    assert ProcrastinateJob.objects.filter(
        task_name="payments.submit_payment", args__payment_id=p.pk
    ).exists()


def test_second_pay_attempt_while_processing_is_409(order):
    _pay(order)
    r = _pay(order, method="pm_ok")
    assert r.status_code == 409 and r.json()["code"] == "payment_in_flight"
    assert Payment.objects.count() == 1


def test_pay_retry_with_same_key_replays(order):
    a = _pay(order, key="pay-1")
    b = _pay(order, key="pay-1")
    assert b.status_code == 202 and b.json() == a.json()
    assert Payment.objects.count() == 1


def test_cannot_pay_someone_elses_order(order, client_for):
    r = client_for(UserFactory()).post(
        f"/api/orders/{order.pk}/pay/", {"payment_method": "pm_ok"}, HTTP_IDEMPOTENCY_KEY="x"
    )
    assert r.status_code == 404


def test_cannot_pay_expired_or_cancelled_order(order):
    order_services.cancel_order(order.pk, actor=order.buyer)
    r = _pay(order)
    assert r.status_code == 409 and r.json()["code"] == "invalid_state"


def test_cannot_pay_after_hold_deadline(order):
    Order.objects.filter(pk=order.pk).update(expires_at=order.created_at)
    assert _pay(order).json()["code"] == "hold_expired"


def test_submit_is_idempotent_at_the_provider(order):
    _pay(order)
    p = Payment.objects.get()
    services.submit_payment(p.pk)
    Payment.objects.filter(pk=p.pk).update(provider_payment_id=None)  # simulate lost write
    services.submit_payment(p.pk)  # job retried after a crash
    assert FakeProviderObject.objects.filter(kind="payment").count() == 1
    p.refresh_from_db()
    assert p.provider_payment_id == FakeProviderObject.objects.get().id


def test_provider_outage_raises_for_job_retry(order):
    _pay(order, method="pm_provider_error")
    with pytest.raises(ProviderError):
        services.submit_payment(Payment.objects.get().pk)


def test_db_forbids_two_successful_payments_for_one_order(order):
    Payment.objects.create(
        order=order,
        event=order.event,
        provider="fake",
        provider_idempotency_key="a",
        payment_method="pm_ok",
        amount_cents=4000,
        currency="USD",
        status=PaymentStatus.SUCCEEDED,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Payment.objects.create(
            order=order,
            event=order.event,
            provider="fake",
            provider_idempotency_key="b",
            payment_method="pm_ok",
            amount_cents=4000,
            currency="USD",
            status=PaymentStatus.SUCCEEDED,
        )


def test_db_forbids_refunding_more_than_paid(order):
    p = Payment.objects.create(
        order=order,
        event=order.event,
        provider="fake",
        provider_idempotency_key="a",
        payment_method="pm_ok",
        amount_cents=4000,
        currency="USD",
        status=PaymentStatus.SUCCEEDED,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Payment.objects.filter(pk=p.pk).update(refunded_cents=4001)


def test_hold_is_not_expired_while_payment_in_grace_period(order, settings):
    _pay(order)
    just_after = order.expires_at + timedelta(seconds=1)
    assert order_services.expire_order(order.pk, now=just_after) is False
    order.refresh_from_db()
    assert order.status == OrderStatus.PENDING

    after_grace = order.expires_at + timedelta(minutes=settings.PAYMENT_GRACE_MINUTES)
    assert order_services.expire_order(order.pk, now=after_grace) is True
    assert ProcrastinateJob.objects.filter(task_name="payments.cancel_stale_payment").exists()


def test_cancel_stale_payment_cancels_at_provider(order):
    _pay(order)
    p = Payment.objects.get()
    services.submit_payment(p.pk)
    services.cancel_stale_payment(p.pk)
    assert FakeProviderObject.objects.get(kind="payment").status == "canceled"


def test_cancel_stale_unsubmitted_payment_fails_locally(order):
    _pay(order)
    services.cancel_stale_payment(Payment.objects.get().pk)
    p = Payment.objects.get()
    assert (p.status, p.failure_reason) == ("failed", "never_submitted")


def test_buyer_cannot_cancel_while_payment_processing(order):
    _pay(order)
    r = order.client.post(f"/api/orders/{order.pk}/cancel/")
    assert r.status_code == 409 and r.json()["code"] == "payment_in_flight"
