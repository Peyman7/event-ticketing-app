from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.catalog.models import Event, TicketType
from apps.core.state_machine import StateMachine


class OrderStatus(models.TextChoices):
    PENDING = "pending"  # tickets held, awaiting payment
    PAID = "paid"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


S = OrderStatus
ORDER_MACHINE = StateMachine(
    "order",
    {
        S.PENDING: {S.PAID, S.EXPIRED, S.CANCELLED},
        S.PAID: {S.PARTIALLY_REFUNDED, S.REFUNDED},
        S.PARTIALLY_REFUNDED: {S.PARTIALLY_REFUNDED, S.REFUNDED},
    },
)
# Orders in these states hold or own inventory.
ACTIVE_STATUSES = (S.PENDING, S.PAID, S.PARTIALLY_REFUNDED)


class Order(models.Model):
    buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders"
    )
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="orders")
    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=S.PENDING)
    total_cents = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    expires_at = models.DateTimeField()
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["buyer", "-created_at"]),
            # The expiry sweeper only ever looks at pending orders; a partial index stays
            # tiny even with millions of historical orders.
            models.Index(
                fields=["expires_at"],
                condition=Q(status="pending"),
                name="order_pending_expiry_idx",
            ),
            # The sales timeline reads only paid orders of one event.
            models.Index(
                fields=["event", "paid_at"],
                condition=Q(paid_at__isnull=False),
                name="order_paid_by_event_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=OrderStatus.values), name="order_status_valid"
            ),
            models.CheckConstraint(condition=Q(total_cents__gte=0), name="order_total_gte_0"),
            models.CheckConstraint(
                condition=~Q(status="paid") | Q(paid_at__isnull=False),
                name="order_paid_has_paid_at",
            ),
        ]

    def __str__(self) -> str:
        return f"Order#{self.pk} {self.status}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    ticket_type = models.ForeignKey(
        TicketType, on_delete=models.PROTECT, related_name="order_items"
    )
    quantity = models.PositiveIntegerField()
    unit_price_cents = models.BigIntegerField()  # price snapshot at reservation time

    class Meta:
        indexes = [models.Index(fields=["ticket_type"])]
        constraints = [
            models.UniqueConstraint(fields=["order", "ticket_type"], name="orderitem_unique_type"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="orderitem_quantity_gt_0"),
            models.CheckConstraint(
                condition=Q(unit_price_cents__gte=0), name="orderitem_price_gte_0"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.quantity} x {self.ticket_type_id}"

    @property
    def line_total_cents(self) -> int:
        return self.quantity * self.unit_price_cents


class TicketStatus(models.TextChoices):
    VALID = "valid"
    CHECKED_IN = "checked_in"
    REFUNDED = "refunded"


class Ticket(models.Model):
    order_item = models.ForeignKey(OrderItem, on_delete=models.PROTECT, related_name="tickets")
    # No standalone FK index: ticket_type_status_cov_idx leads with ticket_type and serves
    # the same lookups, so a second index would only add write cost at on-sale time.
    ticket_type = models.ForeignKey(
        TicketType, on_delete=models.PROTECT, related_name="tickets", db_index=False
    )
    # Denormalised so check-in can scope a scan to an event without joins.
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="tickets")
    code = models.CharField(max_length=32, unique=True)
    # Immutable snapshot of what was paid for this seat (= its OrderItem's unit price).
    # Denormalised so reporting and refunds aggregate tickets without joining order items.
    price_cents = models.BigIntegerField()
    status = models.CharField(
        max_length=20, choices=TicketStatus.choices, default=TicketStatus.VALID
    )
    checked_in_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            # Covering index: the per-type sales report is an index-only scan.
            models.Index(
                fields=["ticket_type", "status"],
                # id too: Django compiles Count("tickets") to COUNT(orders_ticket.id).
                include=["price_cents", "id"],
                name="ticket_type_status_cov_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=TicketStatus.values), name="ticket_status_valid"
            ),
            models.CheckConstraint(condition=Q(price_cents__gte=0), name="ticket_price_gte_0"),
            models.CheckConstraint(
                condition=(Q(status="checked_in") & Q(checked_in_at__isnull=False))
                | (~Q(status="checked_in") & Q(checked_in_at__isnull=True)),
                name="ticket_checked_in_at_consistent",
            ),
        ]

    def __str__(self) -> str:
        return f"Ticket {self.code} {self.status}"
