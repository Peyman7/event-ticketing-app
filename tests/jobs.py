"""Run queued Procrastinate jobs synchronously in tests, in id order, like a worker would.

fake_provider.deliver jobs are routed through the Django test client instead of real
HTTP, so the full chain pay -> submit -> settle -> signed webhook -> process runs.
"""

from __future__ import annotations

import json

from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.payments import signing


def post_webhook(client, event: dict, secret: str | None = None, timestamp: int | None = None):
    from django.conf import settings

    body = json.dumps(event, separators=(",", ":")).encode()
    sig = signing.sign(body, secret or settings.PAYMENT_WEBHOOK_SECRET, timestamp)
    return client.post(
        "/api/webhooks/payments/", body, content_type="application/json", HTTP_X_SIGNATURE=sig
    )


def drain(client, *, skip=("orders.expire_order",), max_rounds=20) -> list[str]:
    done: set[int] = set()
    ran: list[str] = []
    for _ in range(max_rounds):
        jobs = [
            j
            for j in ProcrastinateJob.objects.filter(status="todo").order_by("id")
            if j.id not in done and j.task_name not in skip
        ]
        if not jobs:
            return ran
        for job in jobs:
            done.add(job.id)
            ran.append(job.task_name)
            if job.task_name == "fake_provider.deliver":
                resp = post_webhook(client, job.args["event"])
                assert resp.status_code == 200, resp.content
            else:
                app.tasks[job.task_name](**job.args)
    raise AssertionError("jobs kept spawning jobs")
