from datetime import timedelta

import factory
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.catalog.models import Event, Organizer, TicketType


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = get_user_model()

    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda u: f"{u.username}@example.com")
    password = factory.django.Password("pw-12345678")


class OrganizerFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Organizer

    name = factory.Sequence(lambda n: f"Organizer {n}")
    owner = factory.SubFactory(UserFactory)


class EventFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Event

    organizer = factory.SubFactory(OrganizerFactory)
    title = factory.Sequence(lambda n: f"Event {n}")
    venue = "Main Hall"
    starts_at = factory.LazyFunction(lambda: timezone.now() + timedelta(days=30))
    currency = "USD"
    is_published = True


class TicketTypeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TicketType

    event = factory.SubFactory(EventFactory)
    name = factory.Sequence(lambda n: f"GA {n}")
    price_cents = 5000
    capacity = 100
    sale_starts_at = factory.LazyFunction(lambda: timezone.now() - timedelta(days=1))
    sale_ends_at = factory.LazyFunction(lambda: timezone.now() + timedelta(days=10))
    max_per_order = 10


class OrderFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "orders.Order"

    buyer = factory.SubFactory(UserFactory)
    event = factory.SubFactory(EventFactory)
    total_cents = 0
    currency = "USD"
    expires_at = factory.LazyFunction(lambda: timezone.now() + timedelta(minutes=10))
