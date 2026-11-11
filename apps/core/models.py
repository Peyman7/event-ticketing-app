from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


class AuditLog(models.Model):
    """Append-only record of every state transition, written in the same transaction."""

    entity_type = models.CharField(max_length=50)
    entity_id = models.BigIntegerField()
    action = models.CharField(max_length=50)
    from_state = models.CharField(max_length=30, blank=True)
    to_state = models.CharField(max_length=30, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    request_id = models.CharField(max_length=64, blank=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["entity_type", "entity_id", "created_at"])]

    def __str__(self) -> str:
        return f"{self.entity_type}#{self.entity_id} {self.from_state}->{self.to_state}"


class IdempotencyKey(models.Model):
    """One row per (user, endpoint scope, client key). See apps/core/idempotency.py."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    scope = models.CharField(max_length=100)
    key = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField(null=True)
    response_body = models.JSONField(null=True, encoder=DjangoJSONEncoder)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "scope", "key"], name="idempotency_unique_key")
        ]

    def __str__(self) -> str:
        return f"{self.scope}:{self.key}"
