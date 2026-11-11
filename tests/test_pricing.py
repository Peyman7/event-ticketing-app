from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from apps.orders.pricing import LineRequest as L
from apps.orders.pricing import PricingError, quote

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@dataclass
class TT:
    id: int
    price_cents: int = 2500
    event_id: int = 1
    max_per_order: int = 4
    sale_starts_at: datetime = NOW - timedelta(days=1)
    sale_ends_at: datetime = NOW + timedelta(days=1)


def _types(*tts):
    return {t.id: t for t in tts}


def test_total_is_integer_sum_of_lines():
    q = quote([L(2, 3), L(1, 2)], _types(TT(1, 1999), TT(2, 4550)), now=NOW)
    assert q.total_cents == 2 * 1999 + 3 * 4550 == 17648
    assert isinstance(q.total_cents, int)
    assert [line.ticket_type_id for line in q.lines] == [1, 2]  # sorted = lock order


def test_free_tickets_total_zero():
    assert quote([L(1, 2)], _types(TT(1, 0)), now=NOW).total_cents == 0


def test_price_is_snapshotted_from_ticket_type():
    tt = TT(1, 1000)
    q = quote([L(1, 1)], _types(tt), now=NOW)
    tt.price_cents = 9999
    assert q.lines[0].unit_price_cents == 1000


@pytest.mark.parametrize(
    "lines,code",
    [
        ([], "empty_order"),
        ([L(1, 0)], "invalid_quantity"),
        ([L(1, -2)], "invalid_quantity"),
        ([L(1, 1), L(1, 1)], "duplicate_ticket_type"),
        ([L(99, 1)], "unknown_ticket_type"),
        ([L(1, 5)], "per_order_limit"),
        ([L(i, 1) for i in range(1, 12)], "too_many_lines"),
    ],
)
def test_rejections(lines, code):
    types = _types(*[TT(i) for i in range(1, 12)])
    with pytest.raises(PricingError) as exc:
        quote(lines, types, now=NOW)
    assert exc.value.code == code


def test_per_order_limit_boundary_is_inclusive():
    assert quote([L(1, 4)], _types(TT(1)), now=NOW).total_cents == 4 * 2500


@pytest.mark.parametrize(
    "starts,ends,ok",
    [
        (NOW, NOW + timedelta(hours=1), True),  # starts exactly now: on sale
        (NOW + timedelta(seconds=1), NOW + timedelta(hours=1), False),  # not yet
        (NOW - timedelta(hours=1), NOW, False),  # ends exactly now: closed
    ],
)
def test_sale_window_edges(starts, ends, ok):
    types = _types(TT(1, sale_starts_at=starts, sale_ends_at=ends))
    if ok:
        quote([L(1, 1)], types, now=NOW)
    else:
        with pytest.raises(PricingError, match="not on sale"):
            quote([L(1, 1)], types, now=NOW)


def test_cannot_mix_events():
    with pytest.raises(PricingError) as exc:
        quote([L(1, 1), L(2, 1)], _types(TT(1, event_id=1), TT(2, event_id=2)), now=NOW)
    assert exc.value.code == "multiple_events"
