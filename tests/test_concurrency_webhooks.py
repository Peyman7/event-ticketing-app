import pytest
from django.test import Client

from apps.orders.models import Order, OrderStatus, Ticket
from apps.orders.pricing import LineRequest as L
from apps.payments import webhooks
from apps.payments.models import WebhookEvent
from apps.payments.providers.fake import FakeProvider
from apps.payments.services import start_payment, submit_payment
from tests.concurrency import race
from tests.factories import TicketTypeFactory, UserFactory
from tests.jobs import post_webhook

pytestmark = [pytest.mark.concurrency, pytest.mark.django_db(transaction=True)]


def test_same_webhook_delivered_and_processed_concurrently_issues_tickets_once():
    from django.db import transaction

    from apps.orders.services import place_order

    tt = TicketTypeFactory(capacity=10)
    buyer = UserFactory()
    with transaction.atomic():
        order = place_order(buyer, [L(tt.id, 2)])
    with transaction.atomic():
        payment = start_payment(order.pk, buyer, "pm_ok")
    submit_payment(payment.pk)
    payment.refresh_from_db()
    event = FakeProvider().settle(payment.provider_payment_id)

    deliveries = race(lambda: post_webhook(Client(), event).status_code, [()] * 10)
    assert [o.value for o in deliveries] == [200] * 10
    assert WebhookEvent.objects.count() == 1

    ev_id = WebhookEvent.objects.get().pk
    processed = race(lambda: webhooks.process(ev_id), [()] * 5)
    assert all(o.error is None for o in processed), [o.error for o in processed]
    assert sorted(o.value for o in processed) == ["already_processed"] * 4 + ["order_paid"]
    assert Order.objects.get(pk=order.pk).status == OrderStatus.PAID
    assert Ticket.objects.count() == 2


def test_reconciliation_racing_the_real_webhook_issues_tickets_once():
    import json

    from django.conf import settings
    from django.db import transaction

    from apps.orders.services import place_order
    from apps.payments import reconcile, signing

    tt = TicketTypeFactory(capacity=10)
    buyer = UserFactory()
    with transaction.atomic():
        order = place_order(buyer, [L(tt.id, 3)])
    with transaction.atomic():
        payment = start_payment(order.pk, buyer, "pm_ok")
    submit_payment(payment.pk)
    payment.refresh_from_db()
    event = FakeProvider().settle(payment.provider_payment_id)
    body = json.dumps(event).encode()
    webhooks.receive(body, signing.sign(body, settings.PAYMENT_WEBHOOK_SECRET))
    ev_id = WebhookEvent.objects.get(event_id=event["id"]).pk

    outcomes = race(
        lambda f: f(),
        [(lambda: webhooks.process(ev_id),), (lambda: reconcile.reconcile_payment(payment.pk),)]
        * 3,
    )
    assert all(o.error is None for o in outcomes), [o.error for o in outcomes]
    assert [o.value for o in outcomes].count("order_paid") == 1
    assert Order.objects.get(pk=order.pk).status == OrderStatus.PAID
    assert Ticket.objects.filter(order_item__order=order).count() == 3
