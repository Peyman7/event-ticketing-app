import itertools

import pytest

from apps.core.models import AuditLog
from apps.core.state_machine import InvalidTransition
from apps.orders.models import ORDER_MACHINE
from apps.orders.models import OrderStatus as S

ALLOWED = {
    (S.PENDING, S.PAID),
    (S.PENDING, S.EXPIRED),
    (S.PENDING, S.CANCELLED),
    (S.PAID, S.PARTIALLY_REFUNDED),
    (S.PAID, S.REFUNDED),
    (S.PARTIALLY_REFUNDED, S.PARTIALLY_REFUNDED),
    (S.PARTIALLY_REFUNDED, S.REFUNDED),
}


@pytest.mark.parametrize("src,dst", list(itertools.product(S.values, S.values)))
def test_transition_table_is_exactly_the_documented_one(src, dst):
    """Exhaustive over all 36 pairs, so an accidental new edge fails a test."""
    assert ORDER_MACHINE.can(src, dst) == ((src, dst) in ALLOWED)


@pytest.mark.parametrize("state", [S.REFUNDED, S.EXPIRED, S.CANCELLED])
def test_terminal_states(state):
    assert ORDER_MACHINE.is_terminal(state)


def test_check_raises_with_states():
    with pytest.raises(InvalidTransition) as exc:
        ORDER_MACHINE.check(S.EXPIRED, S.PAID)
    assert (exc.value.from_state, exc.value.to_state) == (S.EXPIRED, S.PAID)


@pytest.mark.django_db
def test_apply_persists_and_audits(order_factory):
    order = order_factory()
    ORDER_MACHINE.apply(order, S.CANCELLED, reason="buyer_cancelled")
    order.refresh_from_db()
    assert order.status == S.CANCELLED
    log = AuditLog.objects.get(entity_type="order", entity_id=order.pk)
    assert (log.from_state, log.to_state, log.data) == (
        S.PENDING,
        S.CANCELLED,
        {"reason": "buyer_cancelled"},
    )


@pytest.mark.django_db
def test_apply_rejects_illegal_and_leaves_row_untouched(order_factory):
    order = order_factory(status=S.EXPIRED)
    with pytest.raises(InvalidTransition):
        ORDER_MACHINE.apply(order, S.PAID)
    order.refresh_from_db()
    assert order.status == S.EXPIRED
    assert not AuditLog.objects.exists()
