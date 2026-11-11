"""Ticket check-in: exactly one successful scan per ticket, even when scans race.

Strategy: one conditional UPDATE on the ticket row, no read-then-write:

    UPDATE orders_ticket SET status='checked_in', checked_in_at=now()
    WHERE code=%s AND event_id=%s AND status='valid'
    RETURNING id

If two scanners race, the second UPDATE blocks on the row lock; when the first commits,
Postgres re-checks the WHERE clause against the *new* row version (EvalPlanQual), sees
status <> 'valid', and updates nothing. Rowcount 1 = admitted, 0 = rejected.

Why raw SQL: the predicate must stay on the ticket's own columns. Adding a join filter
via the ORM (e.g. event__organizer__owner=user) makes Django emit
`WHERE id IN (SELECT ... JOIN ...)`, and Postgres does *not* re-run that subquery on the
re-check, so both racing scans could succeed. Permission is checked separately first.
RETURNING also gives us the row without a second query.

Contrast with inventory, which needs SELECT ... FOR UPDATE because it must read a
value (reserved_count), decide in Python, then write several rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db import connection, transaction
from django.utils import timezone

from apps.core.audit import audit

from .models import Ticket, TicketStatus

CHECKIN_SQL = """
UPDATE orders_ticket
SET status = 'checked_in', checked_in_at = %(now)s
WHERE code = %(code)s AND event_id = %(event_id)s AND status = 'valid'
RETURNING id
"""


@dataclass(frozen=True)
class CheckinResult:
    admitted: bool
    code: str  # admitted | already_checked_in | ticket_refunded | unknown_ticket
    ticket: Ticket | None = None
    checked_in_at: datetime | None = None


def check_in(event_id: int, code: str, actor) -> CheckinResult:
    """Caller must already have verified actor may scan for event_id."""
    now = timezone.now()
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(CHECKIN_SQL, {"now": now, "code": code, "event_id": event_id})
            row = cur.fetchone()
        if row:
            ticket = Ticket.objects.get(pk=row[0])
            audit(ticket, "transition", from_state="valid", to_state="checked_in", actor=actor)
            return CheckinResult(True, "admitted", ticket, now)

    # Rejected: work out why (informational read, after the decisive UPDATE).
    ticket = Ticket.objects.filter(code=code, event_id=event_id).first()
    if ticket is None:
        return CheckinResult(False, "unknown_ticket")
    if ticket.status == TicketStatus.CHECKED_IN:
        return CheckinResult(False, "already_checked_in", ticket, ticket.checked_in_at)
    return CheckinResult(False, "ticket_refunded", ticket)
