"""Correctness check after a load test: did anything oversell, double-charge or leak?"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Sum

from apps.catalog.models import TicketType
from apps.core.models import IdempotencyKey
from apps.orders.models import Order, OrderItem, Ticket
from apps.orders.reconcile import inventory_mismatches
from apps.payments.models import Payment, WebhookEvent


class Command(BaseCommand):
    help = "Summarise and verify invariants for the load-test event."

    def add_arguments(self, parser):
        parser.add_argument("--setup", default="loadtest/setup.json")

    def handle(self, *args, setup, **opts):
        event_id = json.loads(Path(setup).read_text())["event_id"]
        types = TicketType.objects.filter(event_id=event_id)
        orders = Order.objects.filter(event_id=event_id)
        out = {
            "capacity": sum(t.capacity for t in types),
            "reserved_count": sum(t.reserved_count for t in types),
            "orders_by_status": dict(orders.values_list("status").annotate(n=Count("id"))),
            "tickets_issued": Ticket.objects.filter(event_id=event_id).count(),
            "seats_in_paid_orders": OrderItem.objects.filter(
                order__event_id=event_id, order__status="paid"
            ).aggregate(n=Sum("quantity"))["n"]
            or 0,
            "payments_by_status": dict(
                Payment.objects.filter(event_id=event_id)
                .values_list("status")
                .annotate(n=Count("id"))
            ),
            "max_succeeded_payments_per_order": max(
                Payment.objects.filter(event_id=event_id, status="succeeded")
                .values("order_id")
                .annotate(n=Count("id"))
                .values_list("n", flat=True),
                default=0,
            ),
            # Every stored 201 under orders.create must point at a distinct order of
            # this event: a duplicate here would mean a retry created a second order.
            "checkout_keys_with_201": len(
                created := list(
                    IdempotencyKey.objects.filter(
                        scope="orders.create", response_status=201, response_body__event=event_id
                    ).values_list("response_body__id", flat=True)
                )
            ),
            "distinct_orders_behind_those_keys": len(set(created)),
            "webhooks": dict(WebhookEvent.objects.values_list("status").annotate(n=Count("id"))),
            "inventory_mismatches": inventory_mismatches(),
        }
        self.stdout.write(json.dumps(out, indent=2, default=str))
        problems = []
        if out["reserved_count"] > out["capacity"]:
            problems.append("OVERSOLD")
        if out["tickets_issued"] != out["seats_in_paid_orders"]:
            problems.append("tickets != seats in paid orders")
        if out["max_succeeded_payments_per_order"] > 1:
            problems.append("DOUBLE CHARGE")
        if out["inventory_mismatches"]:
            problems.append("inventory mismatch")
        if out["checkout_keys_with_201"] != out["distinct_orders_behind_those_keys"]:
            problems.append("DUPLICATE ORDER FROM RETRY")
        if out["distinct_orders_behind_those_keys"] != orders.count():
            problems.append("orders created outside the idempotency path")
        if problems:
            raise CommandError(", ".join(problems))
        self.stdout.write(self.style.SUCCESS("invariants hold"))
