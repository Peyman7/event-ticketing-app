"""Pure order validation + pricing. No DB access, so it is cheap to unit-test exhaustively.

All amounts are integer cents. Python ints don't overflow; Postgres BIGINT holds ~9.2e18
cents, far beyond any order total we would accept.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

MAX_LINES_PER_ORDER = 10


class PricingError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class PricedTicketType(Protocol):
    id: int
    event_id: int
    price_cents: int
    max_per_order: int
    sale_starts_at: datetime
    sale_ends_at: datetime


@dataclass(frozen=True)
class LineRequest:
    ticket_type_id: int
    quantity: int


@dataclass(frozen=True)
class PricedLine:
    ticket_type_id: int
    quantity: int
    unit_price_cents: int

    @property
    def total_cents(self) -> int:
        return self.quantity * self.unit_price_cents


@dataclass(frozen=True)
class Quote:
    event_id: int
    lines: tuple[PricedLine, ...]

    @property
    def total_cents(self) -> int:
        return sum(line.total_cents for line in self.lines)


def normalize_lines(lines: Iterable[LineRequest]) -> list[LineRequest]:
    """Reject malformed carts. Sorted by ticket_type_id, which is also the lock order."""
    lines = list(lines)
    if not lines:
        raise PricingError("empty_order", "An order needs at least one line.")
    if len(lines) > MAX_LINES_PER_ORDER:
        raise PricingError("too_many_lines", f"At most {MAX_LINES_PER_ORDER} lines per order.")
    ids = [line.ticket_type_id for line in lines]
    if len(set(ids)) != len(ids):
        raise PricingError("duplicate_ticket_type", "Each ticket type may appear once.")
    for line in lines:
        if line.quantity < 1:
            raise PricingError("invalid_quantity", "Quantity must be at least 1.")
    return sorted(lines, key=lambda line: line.ticket_type_id)


def quote(
    lines: Iterable[LineRequest],
    ticket_types: Mapping[int, PricedTicketType],
    *,
    now: datetime,
) -> Quote:
    """Validate a cart against ticket-type rules and price it with current prices."""
    lines = normalize_lines(lines)
    missing = [line.ticket_type_id for line in lines if line.ticket_type_id not in ticket_types]
    if missing:
        raise PricingError("unknown_ticket_type", f"Unknown ticket type(s): {missing}")

    event_ids = {ticket_types[line.ticket_type_id].event_id for line in lines}
    if len(event_ids) != 1:
        raise PricingError("multiple_events", "All tickets in an order must be for one event.")

    priced = []
    for line in lines:
        tt = ticket_types[line.ticket_type_id]
        if not (tt.sale_starts_at <= now < tt.sale_ends_at):
            raise PricingError("not_on_sale", f"Ticket type {tt.id} is not on sale.")
        if line.quantity > tt.max_per_order:
            raise PricingError(
                "per_order_limit", f"At most {tt.max_per_order} of ticket type {tt.id} per order."
            )
        priced.append(PricedLine(tt.id, line.quantity, tt.price_cents))
    return Quote(event_id=event_ids.pop(), lines=tuple(priced))
