"""The database itself must reject invalid rows, independent of application code.

`.update()` bypasses model validation and goes straight to SQL, so these tests prove the
CHECK constraints are real.
"""

import pytest
from django.db import IntegrityError, transaction
from django.db.models import F

from apps.catalog.models import Event, TicketType
from tests.factories import EventFactory, TicketTypeFactory

pytestmark = pytest.mark.django_db


def _assert_rejected(fn):
    with pytest.raises(IntegrityError), transaction.atomic():
        fn()


def test_reserved_count_cannot_exceed_capacity():
    tt = TicketTypeFactory(capacity=5)
    _assert_rejected(lambda: TicketType.objects.filter(pk=tt.pk).update(reserved_count=6))
    TicketType.objects.filter(pk=tt.pk).update(reserved_count=5)  # boundary is fine


def test_reserved_count_cannot_go_negative():
    tt = TicketTypeFactory()
    # PositiveIntegerField adds its own CHECK too; our named constraint covers it either way.
    _assert_rejected(
        lambda: TicketType.objects.filter(pk=tt.pk).update(reserved_count=F("reserved_count") - 1)
    )


def test_negative_price_rejected():
    tt = TicketTypeFactory()
    _assert_rejected(lambda: TicketType.objects.filter(pk=tt.pk).update(price_cents=-1))


def test_sale_window_must_be_ordered():
    tt = TicketTypeFactory()
    _assert_rejected(
        lambda: TicketType.objects.filter(pk=tt.pk).update(sale_ends_at=F("sale_starts_at"))
    )


def test_max_per_order_at_least_one():
    tt = TicketTypeFactory()
    _assert_rejected(lambda: TicketType.objects.filter(pk=tt.pk).update(max_per_order=0))


def test_ticket_type_names_unique_per_event():
    tt = TicketTypeFactory(name="VIP")
    _assert_rejected(lambda: TicketTypeFactory(event=tt.event, name="VIP"))
    TicketTypeFactory(name="VIP")  # same name on another event is fine


def test_currency_must_be_iso_code():
    ev = EventFactory()
    _assert_rejected(lambda: Event.objects.filter(pk=ev.pk).update(currency="usd"))
