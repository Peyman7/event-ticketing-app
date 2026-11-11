"""The webhook never arrives: reconciliation asks the provider and applies the result."""

from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from apps.orders.models import Order, Ticket
from apps.orders.pricing import LineRequest
from apps.orders.reconcile import inventory_mismatches
from apps.orders.services import place_order
from apps.payments import reconcile, webhooks
from apps.payments.models import Payment, WebhookEvent
from apps.payments.providers.fake import FakeProvider
from apps.payments.services import start_payment, submit_payment
from tests.factories import TicketTypeFactory, UserFactory
from tests.jobs import post_webhook

pytestmark = pytest.mark.django_db
LATER = timezone.now() + timedelta(minutes=5)


def _submitted(method="pm_ok"):
    tt = TicketTypeFactory(capacity=10)
    buyer = UserFactory()
    with transaction.atomic():
        order = place_order(buyer, [LineRequest(tt.id, 2)])
    with transaction.atomic():
        payment = start_payment(order.pk, buyer, method)
    submit_payment(payment.pk)
    return order, Payment.objects.get(pk=payment.pk)


def test_success_without_webhook_is_reconciled_and_tickets_issued():
    order, payment = _submitted()
    FakeProvider().settle(payment.provider_payment_id)  # provider settled; webhook lost

    assert reconcile.reconcile_stuck_payments(now=LATER) == 1
    order.refresh_from_db()
    payment.refresh_from_db()
    assert (order.status, payment.status) == ("paid", "succeeded")
    assert Ticket.objects.filter(order_item__order=order).count() == 2
    assert inventory_mismatches() == []


def test_decline_without_webhook_is_reconciled():
    order, payment = _submitted("pm_declined")
    FakeProvider().settle(payment.provider_payment_id)
    reconcile.reconcile_stuck_payments(now=LATER)
    payment.refresh_from_db()
    assert payment.status == "failed"
    assert Order.objects.get(pk=order.pk).status == "pending"


def test_still_processing_and_fresh_payments_are_left_alone():
    _, payment = _submitted("pm_delayed")  # provider hasn't settled yet
    assert reconcile.reconcile_payment(payment.pk) == "still_processing"
    assert reconcile.reconcile_stuck_payments(now=timezone.now()) == 0  # not stale yet
    payment.refresh_from_db()
    assert payment.status == "processing"


def test_late_webhook_after_reconciliation_is_a_stale_noop(client):
    order, payment = _submitted()
    event = FakeProvider().settle(payment.provider_payment_id)
    reconcile.reconcile_stuck_payments(now=LATER)

    post_webhook(client, event)  # the original webhook finally shows up
    assert webhooks.process(WebhookEvent.objects.get().pk) == "stale:succeeded->succeeded"
    assert Ticket.objects.filter(order_item__order=order).count() == 2


def test_reconcile_is_idempotent():
    _, payment = _submitted()
    FakeProvider().settle(payment.provider_payment_id)
    assert reconcile.reconcile_payment(payment.pk) == "order_paid"
    assert reconcile.reconcile_payment(payment.pk) == "skipped"
    assert Ticket.objects.count() == 2
