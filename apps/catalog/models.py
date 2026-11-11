"""Organizers, events and ticket types.

Money: integer cents + ISO-4217 currency code on the Event. Every ticket type of an
event shares the event's currency, so an order (one event) never mixes currencies.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q


class Organizer(models.Model):
    name = models.CharField(max_length=200)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="organizers"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.name


class Event(models.Model):
    organizer = models.ForeignKey(Organizer, on_delete=models.PROTECT, related_name="events")
    title = models.CharField(max_length=200)
    venue = models.CharField(max_length=200, blank=True)
    starts_at = models.DateTimeField()
    currency = models.CharField(max_length=3, default="USD")
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["organizer", "starts_at"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(currency__regex=r"^[A-Z]{3}$"), name="event_currency_iso4217"
            ),
        ]

    def __str__(self) -> str:
        return self.title


class TicketType(models.Model):
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="ticket_types")
    name = models.CharField(max_length=100)
    price_cents = models.BigIntegerField()
    capacity = models.PositiveIntegerField()
    # held + sold. Maintained under a row lock by apps.orders.inventory; the CHECK
    # constraint below makes overselling impossible even if that code had a bug.
    reserved_count = models.PositiveIntegerField(default=0)
    sale_starts_at = models.DateTimeField()
    sale_ends_at = models.DateTimeField()
    max_per_order = models.PositiveSmallIntegerField(default=10)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "name"], name="tickettype_unique_name"),
            models.CheckConstraint(condition=Q(price_cents__gte=0), name="tickettype_price_gte_0"),
            models.CheckConstraint(
                condition=Q(reserved_count__gte=0) & Q(reserved_count__lte=F("capacity")),
                name="tickettype_reserved_within_capacity",
            ),
            models.CheckConstraint(
                condition=Q(sale_ends_at__gt=F("sale_starts_at")),
                name="tickettype_sale_window_valid",
            ),
            models.CheckConstraint(
                condition=Q(max_per_order__gte=1), name="tickettype_max_per_order_gte_1"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.event_id}:{self.name}"

    @property
    def available(self) -> int:
        return self.capacity - self.reserved_count
