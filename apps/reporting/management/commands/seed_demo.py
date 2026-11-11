"""Bulk demo data for EXPLAIN ANALYZE and manual testing.

Builds a consistent world (reserved_count == held + sold, payments match orders) quickly
with bulk_create. Shape: each event has GA + VIP; ~85% of capacity sold with an on-sale
spike, a few % held by pending orders, ~2% of sold tickets refunded, ~40% checked in,
plus 3x as many historical expired/cancelled orders (the bulk of any real orders table).
"""

import random
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.catalog.models import Event, Organizer, TicketType
from apps.orders.models import Order, OrderItem, Ticket
from apps.orders.reconcile import inventory_mismatches
from apps.payments.models import Payment

BATCH = 5000


class Command(BaseCommand):
    help = "Seed demo data (organizers, events, orders, tickets, payments)."

    def add_arguments(self, parser):
        parser.add_argument("--events", type=int, default=20)
        parser.add_argument("--capacity", type=int, default=5000, help="per ticket type")
        parser.add_argument("--buyers", type=int, default=5000)
        parser.add_argument("--seed", type=int, default=7)

    def handle(self, *args, events, capacity, buyers, seed, **opts):
        rng = random.Random(seed)
        now = timezone.now()
        pw = make_password("demo-password")
        run = secrets.token_hex(3)

        # Keep simulated created_at values instead of now() (seeding only).
        created_field = Order._meta.get_field("created_at")
        created_field.auto_now_add = False
        try:
            self._seed(rng, now, pw, run, events, capacity, buyers)
        finally:
            created_field.auto_now_add = True
        bad = inventory_mismatches()
        if bad:
            raise RuntimeError(f"seed produced inconsistent inventory: {bad[:3]}")
        self.stdout.write(
            self.style.SUCCESS(
                f"seeded {events} events, organizer=organizer_{run} (password demo-password); "
                f"orders={Order.objects.count()} tickets={Ticket.objects.count()}"
            )
        )

    def _seed(self, rng, now, pw, run, events, capacity, buyers):
        User = get_user_model()
        with transaction.atomic():
            owner = User.objects.create(
                username=f"organizer_{run}", email="org@example.com", password=pw
            )
            org = Organizer.objects.create(name=f"Demo Promotions {run}", owner=owner)
            users = User.objects.bulk_create(
                User(username=f"buyer_{run}_{i}", email=f"b{i}@example.com", password=pw)
                for i in range(buyers)
            )
            for e in range(events):
                self._event(rng, org, users, now, e, capacity)
        bad = inventory_mismatches()
        if bad:
            raise RuntimeError(f"seed produced inconsistent inventory: {bad[:3]}")
        self.stdout.write(
            self.style.SUCCESS(
                f"seeded {events} events, organizer={owner.username} (password demo-password); "
                f"orders={Order.objects.count()} tickets={Ticket.objects.count()}"
            )
        )

    def _event(self, rng, org, users, now, idx, capacity):
        on_sale = now - timedelta(days=rng.randint(2, 20))
        ev = Event.objects.create(
            organizer=org,
            title=f"Demo Show {idx}",
            venue="Arena",
            starts_at=now + timedelta(days=rng.randint(1, 60)),
            currency="USD",
            is_published=True,
        )
        for name, price in (("GA", 4500), ("VIP", 15000)):
            tt = TicketType.objects.create(
                event=ev,
                name=name,
                price_cents=price,
                capacity=capacity,
                sale_starts_at=on_sale,
                sale_ends_at=ev.starts_at,
                max_per_order=6,
            )
            self._fill(rng, tt, users, now, on_sale)

    def _fill(self, rng, tt, users, now, on_sale):
        target_sold, target_held = int(tt.capacity * 0.85), int(tt.capacity * 0.03)
        orders, plan = [], []  # plan: (order, qty, kind)
        sold = held = 0
        window = (now - on_sale).total_seconds()

        def created_at():
            # on-sale spike: 60% of orders in the first hour, rest spread out
            secs = rng.uniform(0, 3600) if rng.random() < 0.6 else rng.uniform(0, window)
            return on_sale + timedelta(seconds=secs)

        def add(kind, qty):
            ts = created_at()
            status = {
                "paid": "paid",
                "held": "pending",
                "expired": "expired",
                "cancelled": "cancelled",
            }[kind]
            # Decide ticket outcomes now, so the order's final status is known at insert.
            ticket_states = []
            if kind == "paid":
                for _ in range(qty):
                    r = rng.random()
                    ticket_states.append(
                        "refunded" if r < 0.02 else "checked_in" if r < 0.42 else "valid"
                    )
                n_ref = ticket_states.count("refunded")
                if n_ref:
                    status = "refunded" if n_ref == qty else "partially_refunded"
            o = Order(
                buyer=rng.choice(users),
                event_id=tt.event_id,
                status=status,
                created_at=ts,
                total_cents=qty * tt.price_cents,
                currency="USD",
                expires_at=(now + timedelta(minutes=8))
                if kind == "held"
                else ts + timedelta(minutes=10),
                paid_at=ts + timedelta(seconds=rng.randint(5, 120)) if kind == "paid" else None,
            )
            orders.append(o)
            plan.append((o, qty, kind, ticket_states))

        while sold < target_sold:
            q = min(rng.randint(1, 4), target_sold - sold)
            add("paid", q)
            sold += q
        while held < target_held:
            q = min(rng.randint(1, 4), target_held - held)
            add("held", q)
            held += q
        for _ in range(len(orders) * 3):
            add(rng.choice(["expired", "expired", "cancelled"]), rng.randint(1, 4))

        Order.objects.bulk_create(orders, batch_size=BATCH)
        items = [
            OrderItem(order=o, ticket_type=tt, quantity=q, unit_price_cents=tt.price_cents)
            for o, q, _, _ in plan
        ]
        OrderItem.objects.bulk_create(items, batch_size=BATCH)

        tickets, payments, refunded_live = [], [], 0
        for (o, _, kind, states), item in zip(plan, items, strict=True):
            if kind != "paid":
                continue
            for st in states:
                cin = o.paid_at + timedelta(days=1) if st == "checked_in" else None
                tickets.append(
                    Ticket(
                        order_item=item,
                        ticket_type=tt,
                        event_id=tt.event_id,
                        price_cents=tt.price_cents,
                        code=secrets.token_urlsafe(12),
                        status=st,
                        checked_in_at=cin,
                    )
                )
            refunded_here = states.count("refunded")
            refunded_live += refunded_here
            payments.append(
                Payment(
                    order=o,
                    event_id=tt.event_id,
                    provider="fake",
                    provider_idempotency_key=secrets.token_hex(16),
                    provider_payment_id=f"pay_{secrets.token_hex(12)}",
                    payment_method="pm_ok",
                    amount_cents=o.total_cents,
                    currency="USD",
                    status="succeeded",
                    refunded_cents=refunded_here * tt.price_cents,
                )
            )
        Ticket.objects.bulk_create(tickets, batch_size=BATCH)
        Payment.objects.bulk_create(payments, batch_size=BATCH)
        TicketType.objects.filter(pk=tt.pk).update(reserved_count=sold - refunded_live + held)
