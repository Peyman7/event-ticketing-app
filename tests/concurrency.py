"""Helper for real races: N threads, each with its own Postgres connection, released at
the same instant by a barrier."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from django.db import connection


@dataclass
class Outcome:
    value: Any = None
    error: BaseException | None = None


def race(fn: Callable[..., Any], args: Sequence[tuple], timeout: float = 60) -> list[Outcome]:
    barrier = threading.Barrier(len(args))

    def worker(a: tuple) -> Outcome:
        try:
            barrier.wait(timeout=timeout)
            return Outcome(value=fn(*a))
        except BaseException as exc:  # collected and asserted on by the test
            return Outcome(error=exc)
        finally:
            connection.close()  # each thread owns a connection; don't leak them

    with ThreadPoolExecutor(max_workers=len(args)) as pool:
        return list(pool.map(worker, args, timeout=timeout))
