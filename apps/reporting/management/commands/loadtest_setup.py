"""Create an on-sale event + N buyers with API tokens for the Locust script.

Registration (PBKDF2 hashing) is deliberately excluded from the load test: we want to
measure checkout, not password hashing.
"""

import json
import secrets
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.catalog.models import Event, Organizer, TicketType


class Command(BaseCommand):
    help = "Prepare data for loadtest/locustfile.py (writes loadtest/setup.json)."

    def add_arguments(self, parser):
        parser.add_argument("--buyers", type=int, default=2000)
        parser.add_argument("--ga", type=int, default=800, help="GA capacity")
        parser.add_argument("--vip", type=int, default=200, help="VIP capacity")
        parser.add_argument("--out", default="loadtest/setup.json")

    def handle(self, *args, buyers, ga, vip, out, **opts):
        User = get_user_model()
        run = secrets.token_hex(3)
        pw = make_password(secrets.token_urlsafe(16))
        now = timezone.now()
        with transaction.atomic():
            owner = User.objects.create(username=f"lt_org_{run}", password=pw)
            org = Organizer.objects.create(name=f"Load Test {run}", owner=owner)
            event = Event.objects.create(
                organizer=org,
                title=f"On-sale spike {run}",
                starts_at=now + timedelta(days=30),
                currency="USD",
                is_published=True,
            )
            types = [
                TicketType.objects.create(
                    event=event,
                    name=name,
                    price_cents=price,
                    capacity=cap,
                    sale_starts_at=now - timedelta(minutes=1),
                    sale_ends_at=now + timedelta(days=1),
                    max_per_order=4,
                )
                for name, price, cap in (("GA", 4500, ga), ("VIP", 15000, vip))
            ]
            users = User.objects.bulk_create(
                User(username=f"lt_{run}_{i}", password=pw) for i in range(buyers)
            )
            tokens = Token.objects.bulk_create(
                Token(user=u, key=secrets.token_hex(20)) for u in users
            )
        data = {
            "event_id": event.pk,
            "ticket_types": [t.pk for t in types],
            "capacity": ga + vip,
            "tokens": [t.key for t in tokens],
        }
        Path(out).write_text(json.dumps(data))
        self.stdout.write(
            self.style.SUCCESS(f"event {event.pk}: {ga} GA + {vip} VIP, {buyers} buyers -> {out}")
        )
