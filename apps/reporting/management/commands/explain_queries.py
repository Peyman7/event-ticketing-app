"""EXPLAIN (ANALYZE, BUFFERS) on the hot-path and reporting queries.

Run after `seed_demo`. Mutating statements run inside a transaction that is rolled back.
Findings are written up in docs/explain-analyze.md.
"""

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Count
from django.utils import timezone

from apps.catalog.models import Event, TicketType
from apps.core.models import IdempotencyKey
from apps.orders.checkin import CHECKIN_SQL
from apps.orders.models import Order, OrderStatus, Ticket
from apps.orders.reconcile import MISMATCH_SQL
from apps.payments.models import Payment
from apps.reporting import queries

OPTS = "ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON"


class Rollback(Exception):
    pass


class Command(BaseCommand):
    help = "Print EXPLAIN ANALYZE plans for the main queries."

    def add_arguments(self, parser):
        parser.add_argument("--only", help="run only queries whose title contains this")
        parser.add_argument(
            "--repeat", type=int, default=7, help="runs per query; reports the median"
        )
        parser.add_argument("--summary", action="store_true", help="only print the timing table")

    def handle(self, *args, only=None, repeat=7, summary=False, **opts):
        self.only, self.repeat, self.summary = only, repeat, summary
        self.timings: list[tuple[str, float]] = []
        with connection.cursor() as cur:
            cur.execute("ANALYZE")  # fresh planner statistics after bulk seeding
        event = (
            Event.objects.annotate(n=Count("tickets")).order_by("-n").first()
        )  # the busiest event
        tt = TicketType.objects.filter(event=event).order_by("id")
        buyer_id = (
            Order.objects.values("buyer_id").annotate(n=Count("id")).order_by("-n")[0]["buyer_id"]
        )
        ticket = Ticket.objects.filter(event=event, status="valid").first()
        payment = Payment.objects.exclude(provider_payment_id=None).first()
        now = timezone.now()

        self._qs(
            "1. reserve: lock ticket types FOR UPDATE",
            TicketType.objects.select_for_update().filter(id__in=[t.id for t in tt]).order_by("id"),
        )
        self._qs(
            "2. sweeper: pending orders past expiry",
            Order.objects.filter(status=OrderStatus.PENDING, expires_at__lte=now)
            .order_by("expires_at")
            .values_list("id", flat=True)[:500],
        )
        self._qs(
            "3. idempotency key lookup",
            IdempotencyKey.objects.filter(user_id=buyer_id, scope="orders.create", key="k"),
        )
        self._qs(
            "4. buyer order history (first page)",
            Order.objects.filter(buyer_id=buyer_id).order_by("-created_at")[:50],
        )
        self._qs(
            "5. webhook: payment by provider id",
            Payment.objects.filter(provider_payment_id=payment.provider_payment_id),
        )
        self._raw(
            "6. check-in conditional UPDATE (rolled back)",
            CHECKIN_SQL,
            {"now": now, "code": ticket.code, "event_id": event.pk},
        )
        self._qs("7. report: sales by ticket type (ORM)", queries.sales_by_ticket_type_qs(event.pk))
        self._qs(
            "8. report: payment totals for event",
            queries.succeeded_payments_qs(event.pk).values("amount_cents", "refunded_cents"),
        )
        self._raw(
            "9. report: hourly sales timeline (raw SQL)",
            queries.TIMELINE_SQL,
            {
                "unit": "hour",
                "step": "1 hour",
                "event_id": event.pk,
                "max_buckets": queries.MAX_BUCKETS,
            },
        )
        self._raw(
            "10. ops: inventory reconciliation (raw SQL, full scan by design)", MISMATCH_SQL, {}
        )
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== median of {repeat} runs ==="))
        for title, ms in self.timings:
            self.stdout.write(f"{ms:9.3f} ms  {title}")

    def _header(self, title):
        if self.only and self.only not in title:
            return False
        self.current = title
        if not self.summary:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {title} ==="))
        return True

    def _record(self, plans: list[str]) -> None:
        """Print the median run's plan; remember its execution time."""

        def ms(plan):
            line = next(ln for ln in plan.splitlines() if ln.startswith("Execution Time"))
            return float(line.split(":")[1].split()[0])

        plans.sort(key=ms)
        median = plans[len(plans) // 2]
        self.timings.append((self.current, ms(median)))
        if not self.summary:
            self.stdout.write(median)

    def _qs(self, title, qs):
        if not self._header(title):
            return
        plans = []
        for _ in range(self.repeat):
            try:
                with transaction.atomic():
                    plans.append(qs.explain(analyze=True, buffers=True, costs=False, summary=True))
                    raise Rollback  # always: EXPLAIN ANALYZE really executes the statement
            except Rollback:
                pass
        self._record(plans)

    def _raw(self, title, sql, params):
        if not self._header(title):
            return
        plans = []
        for _ in range(self.repeat):
            try:
                with transaction.atomic(), connection.cursor() as cur:
                    cur.execute(f"EXPLAIN ({OPTS}) {sql}", params)
                    plans.append("\n".join(r[0] for r in cur.fetchall()))
                    raise Rollback
            except Rollback:
                pass
        self._record(plans)
