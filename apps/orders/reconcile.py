"""Inventory reconciliation, written as raw SQL on purpose.

Why raw SQL: it joins two independent aggregates (holds from pending order items, sales
from live tickets) against the counter column. That is one readable statement in SQL;
in the ORM it becomes two Subquery/OuterRef annotations with Coalesce that obscure the
intent. It's also the query an on-call engineer would paste into psql, so keeping it as
plain SQL keeps one source of truth.

Used by the concurrency tests (an invariant must hold after every race) and by
`manage.py check_inventory`.
"""

from django.db import connection

MISMATCH_SQL = """
SELECT tt.id,
       tt.capacity,
       tt.reserved_count,
       COALESCE(h.held, 0) AS held,
       COALESCE(s.sold, 0) AS sold
FROM catalog_tickettype tt
LEFT JOIN (
    SELECT oi.ticket_type_id, SUM(oi.quantity) AS held
    FROM orders_orderitem oi
    JOIN orders_order o ON o.id = oi.order_id
    WHERE o.status = 'pending'
    GROUP BY oi.ticket_type_id
) h ON h.ticket_type_id = tt.id
LEFT JOIN (
    SELECT t.ticket_type_id, COUNT(*) AS sold
    FROM orders_ticket t
    WHERE t.status IN ('valid', 'checked_in')
    GROUP BY t.ticket_type_id
) s ON s.ticket_type_id = tt.id
WHERE tt.reserved_count <> COALESCE(h.held, 0) + COALESCE(s.sold, 0)
ORDER BY tt.id
"""


def inventory_mismatches() -> list[dict]:
    with connection.cursor() as cur:
        cur.execute(MISMATCH_SQL)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
