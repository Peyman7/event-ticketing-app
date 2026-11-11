"""Per-request / per-job correlation ids carried in contextvars.

contextvars (not thread-locals) so the same code works for sync requests, worker jobs
and threads started inside tests.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
order_id_var: ContextVar[int | None] = ContextVar("order_id", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex


def get_request_id() -> str | None:
    return request_id_var.get()


def bind_order(order_id: int | None) -> None:
    """Attach an order id to every log line emitted for the rest of this request/job."""
    order_id_var.set(order_id)


@contextlib.contextmanager
def correlation(request_id: str | None = None, order_id: int | None = None) -> Iterator[None]:
    """Used by background jobs to restore the request id that enqueued them."""
    rid_token = request_id_var.set(request_id or new_request_id())
    oid_token = order_id_var.set(order_id)
    try:
        yield
    finally:
        request_id_var.reset(rid_token)
        order_id_var.reset(oid_token)
