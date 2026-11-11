from django.conf import settings
from django.db import models
from django.db.models import F, Q

from apps.core.state_machine import StateMachine
from apps.orders.models import Order


class PaymentStatus(models.TextChoices):
    PROCESSING = "processing"  # created; submitted (or about to be) to the provider
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


P = PaymentStatus
# Terminal states never regress: a stale "processing" or "failed" event arriving after
# "succeeded" is simply an illegal transition and gets ignored.
PAYMENT_MACHINE = StateMachine(
    "payment", {P.PROCESSING: {P.PROCESSING, P.SUCCEEDED, P.FAILED, P.CANCELED}}
)


class Payment(models.Model):
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="payments")
    # Denormalised from order.event (immutable) so per-event cash totals don't scan every
    # organizer's payments (docs/explain-analyze.md, query 8).
    event = models.ForeignKey("catalog.Event", on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=30)
    # Sent to the provider so a retried submission can never create a second charge.
    provider_idempotency_key = models.CharField(max_length=64, unique=True)
    provider_payment_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    payment_method = models.CharField(max_length=64)
    amount_cents = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=P.PROCESSING)
    refunded_cents = models.BigIntegerField(default=0)
    failure_reason = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
            # Per-event cash totals: index-only scan over succeeded payments of one event.
            models.Index(
                fields=["event"],
                include=["amount_cents", "refunded_cents"],
                condition=Q(status="succeeded"),
                name="payment_succeeded_by_event_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=PaymentStatus.values), name="payment_status_valid"
            ),
            models.CheckConstraint(condition=Q(amount_cents__gt=0), name="payment_amount_gt_0"),
            # Last line of defence against refunding more than was paid.
            models.CheckConstraint(
                condition=Q(refunded_cents__gte=0) & Q(refunded_cents__lte=F("amount_cents")),
                name="payment_refund_within_amount",
            ),
            models.CheckConstraint(
                condition=Q(refunded_cents=0) | Q(status="succeeded"),
                name="payment_refund_only_if_succeeded",
            ),
            # At most one successful charge per order: double-charging is a DB error.
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(status="succeeded"),
                name="payment_one_success_per_order",
            ),
            # At most one attempt in flight per order.
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(status="processing"),
                name="payment_one_in_flight_per_order",
            ),
        ]

    def __str__(self) -> str:
        return f"Payment#{self.pk} {self.status} {self.amount_cents}{self.currency}"


class FakeProviderObject(models.Model):
    """The fake provider's *own* storage (stands in for Stripe's database). Our domain
    code never reads it directly; it only talks to the provider via the interface."""

    id = models.CharField(primary_key=True, max_length=40)
    kind = models.CharField(max_length=20)  # payment | refund
    idempotency_key = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=20)
    data = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}"


class RefundStatus(models.TextChoices):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


REFUND_MACHINE = StateMachine(
    "refund", {RefundStatus.PENDING: {RefundStatus.SUCCEEDED, RefundStatus.FAILED}}
)


class Refund(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    amount_cents = models.BigIntegerField()
    status = models.CharField(
        max_length=20, choices=RefundStatus.choices, default=RefundStatus.PENDING
    )
    reason = models.CharField(max_length=50)  # requested_by_organizer | late_payment
    provider_idempotency_key = models.CharField(max_length=64, unique=True)
    provider_refund_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    tickets = models.ManyToManyField("orders.Ticket", blank=True, related_name="refunds")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount_cents__gt=0), name="refund_amount_gt_0"),
            models.CheckConstraint(
                condition=Q(status__in=RefundStatus.values), name="refund_status_valid"
            ),
        ]

    def __str__(self) -> str:
        return f"Refund#{self.pk} {self.amount_cents} {self.status}"


class WebhookStatus(models.TextChoices):
    RECEIVED = "received"  # stored, processing job enqueued
    PROCESSED = "processed"
    IGNORED = "ignored"  # duplicate-by-content, stale/out-of-order, unknown object


class WebhookEvent(models.Model):
    provider = models.CharField(max_length=30)
    event_id = models.CharField(max_length=100)
    type = models.CharField(max_length=50)
    payload = models.JSONField()
    status = models.CharField(
        max_length=20, choices=WebhookStatus.choices, default=WebhookStatus.RECEIVED
    )
    result = models.CharField(max_length=100, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # Dedupe: the provider retries deliveries with the same event id.
            models.UniqueConstraint(fields=["provider", "event_id"], name="webhook_unique_event"),
        ]
        indexes = [models.Index(fields=["status", "received_at"])]

    def __str__(self) -> str:
        return f"{self.provider}:{self.event_id} {self.type}"
