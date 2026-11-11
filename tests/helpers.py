"""Build domain states through the real services (no shortcuts that skip invariants)."""

from __future__ import annotations

import json

from django.conf import settings
from django.db import transaction

from apps.orders.models import Order
from apps.orders.pricing import LineRequest
from apps.orders.services import place_order
from apps.payments import signing, webhooks
from apps.payments.models import Payment, WebhookEvent
from apps.payments.providers.fake import FakeProvider
from apps.payments.services import start_payment, submit_payment
from tests.factories import UserFactory


def make_paid_order(lines: list[tuple], buyer=None) -> Order:
    """lines: [(ticket_type, qty), ...] -> a paid Order with issued tickets."""
    buyer = buyer or UserFactory()
    with transaction.atomic():
        order = place_order(buyer, [LineRequest(tt.id, qty) for tt, qty in lines])
    with transaction.atomic():
        payment = start_payment(order.pk, buyer, "pm_ok")
    submit_payment(payment.pk)
    payment = Payment.objects.get(pk=payment.pk)
    event = FakeProvider().settle(payment.provider_payment_id)
    body = json.dumps(event).encode()
    webhooks.receive(body, signing.sign(body, settings.PAYMENT_WEBHOOK_SECRET))
    webhooks.process(WebhookEvent.objects.get(event_id=event["id"]).pk)
    order.refresh_from_db()
    assert order.status == "paid", order.status
    return order
