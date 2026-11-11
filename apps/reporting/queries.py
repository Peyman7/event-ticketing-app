"""Organizer reporting queries.

sales_by_ticket_type: ORM, one query, index-only scan on ticket_type_status_cov_idx.
Pitfall avoided: annotating Count over `tickets` AND Sum over `order_items` in the same
query joins two one-to-many relations and multiplies rows (every number inflated). We
only touch tickets (price is snapshotted on the ticket) and derive
held = reserved_count - sold from the inventory invariant (checked by reconcile.py).

sales_timeline: raw SQL, because it needs generate_series to emit empty buckets (hours
with zero sales) and date_trunc on a parameterised unit. In the ORM that becomes a
Python-side gap-filling loop; in SQL it is one statement the planner can optimise.
"""

from __future__ import annotations

from django.db import connection
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce

from apps.catalog.models import TicketType
from apps.orders.models import TicketStatus
from apps.payments.models import Payment, PaymentStatus

LIVE = [TicketStatus.VALID, TicketStatus.CHECKED_IN]


REPORT_FIELDS = (
    "id",
    "name",
    "price_cents",
    "capacity",
    "held",
    "sold",
    "checked_in",
    "refunded",
    "gross_cents",
    "refunded_cents",
    "net_cents",
)


def sales_by_ticket_type_qs(event_id: int):
    zero = 0
    return (
        TicketType.objects.filter(event_id=event_id)
        .annotate(
            sold=Count("tickets", filter=Q(tickets__status__in=LIVE)),
            checked_in=Count("tickets", filter=Q(tickets__status=TicketStatus.CHECKED_IN)),
            refunded=Count("tickets", filter=Q(tickets__status=TicketStatus.REFUNDED)),
            gross_cents=Coalesce(Sum("tickets__price_cents"), zero),
            refunded_cents=Coalesce(
                Sum(
                    "tickets__price_cents",
                    filter=Q(tickets__status=TicketStatus.REFUNDED),
                ),
                zero,
            ),
        )
        .annotate(
            held=F("reserved_count") - F("sold"), net_cents=F("gross_cents") - F("refunded_cents")
        )
        .order_by("id")
        .values(*REPORT_FIELDS)
    )


def sales_by_ticket_type(event_id: int) -> list[dict]:
    return list(sales_by_ticket_type_qs(event_id))


def succeeded_payments_qs(event_id: int):
    """Served by the partial covering index payment_succeeded_by_event_idx."""
    return Payment.objects.filter(event_id=event_id, status=PaymentStatus.SUCCEEDED)


def payment_totals(event_id: int) -> dict:
    """Cash view from the payments ledger (includes auto-refunded late payments)."""
    agg = succeeded_payments_qs(event_id).aggregate(
        collected_cents=Coalesce(Sum("amount_cents"), 0),
        refunded_cents=Coalesce(Sum("refunded_cents"), 0),
        payments=Count("id"),
    )
    agg["net_cents"] = agg["collected_cents"] - agg["refunded_cents"]
    return agg


def event_report(event_id: int) -> dict:
    types = sales_by_ticket_type(event_id)
    keys = [
        "capacity",
        "held",
        "sold",
        "checked_in",
        "refunded",
        "gross_cents",
        "refunded_cents",
        "net_cents",
    ]
    totals = {k: sum(t[k] for t in types) for k in keys}
    totals["attendance_rate"] = (
        round(totals["checked_in"] / totals["sold"], 4) if totals["sold"] else 0.0
    )
    return {"ticket_types": types, "totals": totals, "payments": payment_totals(event_id)}


BUCKETS = {"hour": "1 hour", "day": "1 day"}
MAX_BUCKETS = 24 * 31

TIMELINE_SQL = """
WITH bounds AS (
    SELECT date_trunc(%(unit)s, MIN(tt.sale_starts_at)) AS lo,
           date_trunc(%(unit)s, LEAST(MAX(tt.sale_ends_at), now())) AS hi
    FROM catalog_tickettype tt
    WHERE tt.event_id = %(event_id)s
),
buckets AS (
    SELECT generate_series(
               GREATEST(lo, hi - (%(max_buckets)s - 1) * %(step)s::interval),
               hi,
               %(step)s::interval) AS bucket
    FROM bounds
),
sales AS (
    -- Driven by the partial index order_paid_by_event_idx. Seats sold = item quantities,
    -- so no join to tickets is needed.
    SELECT date_trunc(%(unit)s, o.paid_at)          AS bucket,
           SUM(oi.quantity)                         AS tickets,
           SUM(oi.quantity * oi.unit_price_cents)   AS gross_cents
    FROM orders_order o
    JOIN orders_orderitem oi ON oi.order_id = o.id
    WHERE o.event_id = %(event_id)s
      AND o.paid_at IS NOT NULL
      AND o.paid_at >= (SELECT MIN(bucket) FROM buckets)
    GROUP BY 1
)
SELECT b.bucket, COALESCE(s.tickets, 0) AS tickets, COALESCE(s.gross_cents, 0) AS gross_cents
FROM buckets b
LEFT JOIN sales s USING (bucket)
ORDER BY b.bucket
"""


def sales_timeline(event_id: int, unit: str = "hour") -> list[dict]:
    if unit not in BUCKETS:
        raise ValueError("unit must be hour or day")
    params = {"unit": unit, "step": BUCKETS[unit], "event_id": event_id, "max_buckets": MAX_BUCKETS}
    with connection.cursor() as cur:
        cur.execute(TIMELINE_SQL, params)
        return [
            {"bucket": b, "tickets": int(n), "gross_cents": int(g)} for b, n, g in cur.fetchall()
        ]
