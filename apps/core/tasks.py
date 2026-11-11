from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from procrastinate.contrib.django import app

from .models import IdempotencyKey


@app.periodic(cron="17 * * * *", periodic_id="cleanup_idempotency_keys")
@app.task(name="core.cleanup_idempotency_keys", queue="maintenance")
def cleanup_idempotency_keys(timestamp: int) -> int:
    """Keys are a retry window, not forever: after the TTL a reused key is a new request."""
    cutoff = timezone.now() - timedelta(hours=settings.IDEMPOTENCY_KEY_TTL_HOURS)
    deleted, _ = IdempotencyKey.objects.filter(created_at__lt=cutoff).delete()
    return deleted
