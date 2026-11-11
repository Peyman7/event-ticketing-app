"""On-sale spike: many buyers hit one event the moment it goes on sale.

Each simulated buyer: view the event -> checkout (1-4 seats, Idempotency-Key) ->
pay (pm_ok) -> poll until the webhook marks the order paid. 10% of checkouts are sent
twice with the same key (a client retry after a timeout); the replay must return the
same order. Buyers who get 409 sold_out stop.

Prepare data first:  docker compose run --rm web python manage.py loadtest_setup
Run:                 make loadtest  (or see docs/load-test.md)
Verify afterwards:   docker compose run --rm web python manage.py loadtest_report
"""

import itertools
import json
import random
import time
import uuid
from pathlib import Path

from locust import HttpUser, between, events, task
from locust.exception import StopUser

SETUP = json.loads((Path(__file__).parent / "setup.json").read_text())
_tokens = itertools.cycle(SETUP["tokens"])
RETRY_RATE = 0.10
PAY_POLL_SECONDS = 20


@events.init_command_line_parser.add_listener
def _(parser):
    parser.add_argument("--retry-rate", type=float, default=RETRY_RATE)


class Buyer(HttpUser):
    wait_time = between(0.05, 0.5)

    def on_start(self):
        self.client.headers["Authorization"] = f"Token {next(_tokens)}"
        self.done = False

    @task
    def buy(self):
        if self.done:
            raise StopUser
        self.client.get(f"/api/events/{SETUP['event_id']}/", name="GET /api/events/{id}/")

        body = {
            "items": [
                {
                    "ticket_type": random.choice(SETUP["ticket_types"]),
                    "quantity": random.randint(1, 4),
                }
            ]
        }
        key = uuid.uuid4().hex
        order = self._checkout(body, key)
        if order is None:
            return
        if random.random() < self.environment.parsed_options.retry_rate:
            self._retry_same_key(body, key, order["id"])

        with self.client.post(
            f"/api/orders/{order['id']}/pay/",
            json={"payment_method": "pm_ok"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
            name="POST /api/orders/{id}/pay/",
            catch_response=True,
        ) as r:
            if r.status_code != 202:
                r.failure(f"pay: {r.status_code} {r.text[:120]}")
                return
        self._wait_paid(order["id"])
        self.done = True  # one successful purchase per buyer

    def _checkout(self, body, key):
        with self.client.post(
            "/api/orders/",
            json=body,
            headers={"Idempotency-Key": key},
            name="POST /api/orders/",
            catch_response=True,
        ) as r:
            if r.status_code == 201:
                return r.json()
            if r.status_code == 409 and r.json().get("code") == "sold_out":
                r.success()  # expected outcome at an on-sale, not an error
                self.done = True
                return None
            r.failure(f"checkout: {r.status_code} {r.text[:120]}")
            return None

    def _retry_same_key(self, body, key, order_id):
        with self.client.post(
            "/api/orders/",
            json=body,
            headers={"Idempotency-Key": key},
            name="POST /api/orders/ (retry same key)",
            catch_response=True,
        ) as r:
            if r.status_code != 201 or r.json().get("id") != order_id:
                r.failure(f"replay mismatch: {r.status_code} {r.text[:120]}")
            elif r.headers.get("Idempotent-Replayed") != "true":
                r.failure("replay not flagged")

    def _wait_paid(self, order_id):
        deadline = time.monotonic() + PAY_POLL_SECONDS
        start = time.monotonic()
        while time.monotonic() < deadline:
            r = self.client.get(f"/api/orders/{order_id}/", name="GET /api/orders/{id}/ (poll)")
            if r.ok and r.json()["status"] == "paid":
                events.request.fire(
                    request_type="ASYNC",
                    name="pay -> webhook -> paid",
                    response_time=(time.monotonic() - start) * 1000,
                    response_length=0,
                    exception=None,
                    context={},
                )
                return
            time.sleep(0.5)
        events.request.fire(
            request_type="ASYNC",
            name="pay -> webhook -> paid",
            response_time=PAY_POLL_SECONDS * 1000,
            response_length=0,
            exception=TimeoutError("order not paid in time"),
            context={},
        )
