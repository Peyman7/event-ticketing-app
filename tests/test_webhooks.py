import json
import time
import uuid
from datetime import timedelta

import pytest
from django.core import mail

from apps.orders import services as order_services
from apps.orders.models import Order, OrderStatus, Ticket
from apps.orders.reconcile import inventory_mismatches
from apps.payments import webhooks
from apps.payments.models import Payment, Refund, WebhookEvent
from apps.payments.providers.fake import FakeProvider, build_event
from tests.factories import TicketTypeFactory, UserFactory
from tests.jobs import drain, post_webhook

pytestmark = pytest.mark.django_db


@pytest.fixture
def buyer_client(client_for):
    user = UserFactory(email="buyer@example.com")
    c = client_for(user)
    c.user = user
    return c


@pytest.fixture
def tt():
    return TicketTypeFactory(capacity=10, price_cents=2500)


def _order(c, tt, qty=2):
    r = c.post(
        "/api/orders/",
        {"items": [{"ticket_type": tt.id, "quantity": qty}]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
    )
    return Order.objects.get(pk=r.json()["id"])


def _pay(c, order, method="pm_ok"):
    r = c.post(
        f"/api/orders/{order.pk}/pay/",
        {"payment_method": method},
        format="json",
        HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
    )
    assert r.status_code == 202, r.json()
    return Payment.objects.get(pk=r.json()["id"])


def _settled_event(payment):
    """Submit to the fake provider and settle, returning the event it would send."""
    from apps.payments.services import submit_payment

    submit_payment(payment.pk)
    payment.refresh_from_db()
    return FakeProvider().settle(payment.provider_payment_id)


# --- happy path, end to end through the job chain -----------------------------------


def test_full_flow_pay_webhook_issues_tickets_and_emails(buyer_client, tt, client):
    order = _order(buyer_client, tt, qty=3)
    _pay(buyer_client, order, "pm_ok")
    ran = drain(client)

    assert ran[:4] == [
        "payments.submit_payment",
        "fake_provider.settle",
        "fake_provider.deliver",
        "payments.process_webhook_event",
    ]
    order.refresh_from_db()
    assert order.status == OrderStatus.PAID and order.paid_at
    tickets = list(Ticket.objects.filter(order_item__order=order))
    assert len(tickets) == 3 and len({t.code for t in tickets}) == 3
    assert len(mail.outbox) == 1 and tickets[0].code in mail.outbox[0].body
    tt.refresh_from_db()
    assert tt.reserved_count == 3  # held -> sold, counter unchanged
    assert inventory_mismatches() == []
    r = buyer_client.get(f"/api/orders/{order.pk}/tickets/")
    assert len(r.json()) == 3


def test_decline_keeps_order_pending_and_allows_retry(buyer_client, tt, client):
    order = _order(buyer_client, tt)
    _pay(buyer_client, order, "pm_declined")
    drain(client)
    order.refresh_from_db()
    p = Payment.objects.get()
    assert (order.status, p.status, p.failure_reason) == ("pending", "failed", "card_declined")
    assert not Ticket.objects.exists()

    _pay(buyer_client, order, "pm_ok")
    drain(client)
    order.refresh_from_db()
    assert order.status == "paid"
    assert Ticket.objects.count() == 2


def test_delayed_payment_settles_later(buyer_client, tt, client):
    order = _order(buyer_client, tt)
    _pay(buyer_client, order, "pm_delayed")
    drain(client, skip=("orders.expire_order", "fake_provider.settle"))
    order.refresh_from_db()
    assert order.status == "pending"  # still waiting on the bank
    drain(client)  # time passes, provider settles
    order.refresh_from_db()
    assert order.status == "paid"


# --- signature verification ------------------------------------------------------------


def _event_for(buyer_client, tt):
    order = _order(buyer_client, tt)
    payment = _pay(buyer_client, order)
    return order, payment, _settled_event(payment)


def test_bad_signature_rejected_and_nothing_stored(buyer_client, tt, client):
    _, _, event = _event_for(buyer_client, tt)
    assert post_webhook(client, event, secret="wrong").status_code == 400
    assert not WebhookEvent.objects.exists()


def test_missing_or_malformed_signature_rejected(buyer_client, tt, client):
    _, _, event = _event_for(buyer_client, tt)
    body = json.dumps(event)
    assert (
        client.post("/api/webhooks/payments/", body, content_type="application/json").status_code
        == 400
    )
    r = client.post(
        "/api/webhooks/payments/", body, content_type="application/json", HTTP_X_SIGNATURE="garbage"
    )
    assert r.status_code == 400


def test_tampered_body_rejected(buyer_client, tt, client, settings):
    from apps.payments import signing

    _, _, event = _event_for(buyer_client, tt)
    body = json.dumps(event).encode()
    sig = signing.sign(body, settings.PAYMENT_WEBHOOK_SECRET)
    event["data"]["object"]["amount"] = 1
    r = client.post(
        "/api/webhooks/payments/",
        json.dumps(event),
        content_type="application/json",
        HTTP_X_SIGNATURE=sig,
    )
    assert r.status_code == 400


def test_old_timestamp_rejected_as_replay(buyer_client, tt, client):
    _, _, event = _event_for(buyer_client, tt)
    r = post_webhook(client, event, timestamp=int(time.time()) - 3600)
    assert r.status_code == 400
    assert not WebhookEvent.objects.exists()


# --- duplicates --------------------------------------------------------------------------


def test_duplicate_delivery_is_stored_once_and_processed_once(buyer_client, tt, client):
    order, _, event = _event_for(buyer_client, tt)
    first = post_webhook(client, event)
    second = post_webhook(client, event)
    assert first.json()["duplicate"] is False and second.json()["duplicate"] is True
    assert WebhookEvent.objects.count() == 1

    ev = WebhookEvent.objects.get()
    assert webhooks.process(ev.pk) == "order_paid"
    assert webhooks.process(ev.pk) == "already_processed"  # job retried
    assert Ticket.objects.filter(order_item__order=order).count() == 2


def test_same_outcome_resent_under_new_event_id_is_ignored(buyer_client, tt, client):
    order, _, event = _event_for(buyer_client, tt)
    post_webhook(client, event)
    resent = {**event, "id": "evt_different"}
    post_webhook(client, resent)
    results = [webhooks.process(e.pk) for e in WebhookEvent.objects.order_by("id")]
    assert results == ["order_paid", "stale:succeeded->succeeded"]
    assert Ticket.objects.filter(order_item__order=order).count() == 2


# --- out of order ------------------------------------------------------------------------


def test_stale_processing_event_after_success_is_ignored(buyer_client, tt, client):
    _, payment, success = _event_for(buyer_client, tt)
    processing = build_event(
        "payment.processing", {**success["data"]["object"], "status": "processing"}
    )
    post_webhook(client, success)
    post_webhook(client, processing)
    results = [webhooks.process(e.pk) for e in WebhookEvent.objects.order_by("id")]
    assert results == ["order_paid", "stale:succeeded->processing"]
    payment.refresh_from_db()
    assert payment.status == "succeeded"


def test_failed_arriving_after_succeeded_does_not_unpay(buyer_client, tt, client):
    order, payment, success = _event_for(buyer_client, tt)
    failed = build_event("payment.failed", {**success["data"]["object"], "status": "failed"})
    post_webhook(client, success)
    post_webhook(client, failed)
    for e in WebhookEvent.objects.order_by("id"):
        webhooks.process(e.pk)
    order.refresh_from_db()
    payment.refresh_from_db()
    assert (order.status, payment.status) == ("paid", "succeeded")


def test_webhook_that_overtakes_submission_still_matches(buyer_client, tt, client):
    """Provider's webhook arrives before our submit job stored the provider id."""
    _, payment, event = _event_for(buyer_client, tt)
    Payment.objects.filter(pk=payment.pk).update(provider_payment_id=None)
    post_webhook(client, event)
    assert webhooks.process(WebhookEvent.objects.get().pk) == "order_paid"
    payment.refresh_from_db()
    assert payment.provider_payment_id == event["data"]["object"]["id"]


def test_unknown_payment_is_ignored_not_errored(client):
    event = build_event(
        "payment.succeeded", {"id": "pay_nope", "amount": 1, "currency": "USD", "metadata": {}}
    )
    post_webhook(client, event)
    assert webhooks.process(WebhookEvent.objects.get().pk) == "unknown_payment"


def test_amount_mismatch_never_marks_paid(buyer_client, tt, client):
    order, _, event = _event_for(buyer_client, tt)
    event["data"]["object"]["amount"] = 1
    post_webhook(client, event)
    assert webhooks.process(WebhookEvent.objects.get().pk) == "amount_mismatch"
    order.refresh_from_db()
    assert order.status == "pending"


# --- late payment ------------------------------------------------------------------------


def test_success_after_expiry_is_auto_refunded_without_tickets(buyer_client, tt, client, settings):
    order, payment, event = _event_for(buyer_client, tt)
    after_grace = order.expires_at + timedelta(minutes=settings.PAYMENT_GRACE_MINUTES + 1)
    assert order_services.expire_order(order.pk, now=after_grace)

    post_webhook(client, event)
    result = webhooks.process(WebhookEvent.objects.get().pk)
    assert result == "late_payment_refunded:expired"
    order.refresh_from_db()
    payment.refresh_from_db()
    assert order.status == "expired"
    assert payment.status == "succeeded" and payment.refunded_cents == payment.amount_cents
    refund = Refund.objects.get()
    assert (refund.reason, refund.amount_cents) == ("late_payment", payment.amount_cents)
    assert not Ticket.objects.exists()
    tt.refresh_from_db()
    assert tt.reserved_count == 0
    assert inventory_mismatches() == []

    drain(client)  # submit refund -> provider -> refund.succeeded webhook
    refund.refresh_from_db()
    assert refund.status == "succeeded" and refund.provider_refund_id
