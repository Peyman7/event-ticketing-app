"""Ticket issuance: only ever called when a payment has been confirmed."""

import secrets

from .models import Order, Ticket


def new_ticket_code() -> str:
    # 96 bits of randomness, URL-safe; uniqueness is also a DB constraint.
    return secrets.token_urlsafe(12)


def issue_tickets(order: Order) -> list[Ticket]:
    """Create one Ticket per held seat. Capacity is unchanged: held -> sold."""
    tickets = [
        Ticket(
            order_item=item,
            ticket_type_id=item.ticket_type_id,
            event_id=order.event_id,
            code=new_ticket_code(),
        )
        for item in order.items.all()
        for _ in range(item.quantity)
    ]
    return Ticket.objects.bulk_create(tickets)
