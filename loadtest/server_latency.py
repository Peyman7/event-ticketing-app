"""Server-side latency per endpoint, from the app's JSON request logs.

Locust measures client-side latency, which includes the load generator's own queueing.
The app's middleware logs `duration_ms` for every request, so comparing the two tells
you whether the server or the test rig is the bottleneck.

    docker compose logs web --since 5m --no-log-prefix | python loadtest/server_latency.py
"""

import json
import re
import sys
from collections import defaultdict

ROUTES = [
    (re.compile(r"^/api/events/\d+/$"), "/api/events/{id}/"),
    (re.compile(r"^/api/orders/\d+/pay/$"), "/api/orders/{id}/pay/"),
    (re.compile(r"^/api/orders/\d+/$"), "/api/orders/{id}/"),
]


def route(path: str) -> str:
    return next((name for rx, name in ROUTES if rx.match(path)), path)


def main() -> None:
    samples: dict[str, list[float]] = defaultdict(list)
    for line in sys.stdin:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("logger") == "apps.request":
            samples[f"{rec['method']} {route(rec['path'])}"].append(rec["duration_ms"])
    print(f"{'endpoint':34s} {'n':>6s} {'p50':>8s} {'p95':>8s} {'p99':>8s} {'max':>8s}  (ms)")
    for name, v in sorted(samples.items()):
        v.sort()

        def q(p, v=v):
            return v[min(len(v) - 1, int(len(v) * p))]

        print(f"{name:34s} {len(v):6d} {q(0.5):8.1f} {q(0.95):8.1f} {q(0.99):8.1f} {v[-1]:8.1f}")


if __name__ == "__main__":
    main()
