"""Background jobs (Procrastinate, stored in Postgres). Thin wrappers over services."""

from procrastinate.contrib.django import app

from apps.core.context import correlation

from . import services


@app.task(name="orders.expire_order", queue="holds")
def expire_order_task(order_id: int, request_id: str | None = None) -> bool:
    with correlation(request_id, order_id):
        return services.expire_order(order_id)


@app.periodic(cron="* * * * *", periodic_id="sweep_expired_holds")
@app.task(name="orders.sweep_expired_holds", queue="holds", queueing_lock="sweep_expired_holds")
def sweep_expired_holds_task(timestamp: int) -> int:
    with correlation():
        return services.sweep_expired_holds()
