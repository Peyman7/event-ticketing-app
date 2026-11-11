from .context import get_request_id
from .models import AuditLog


def audit(entity, action: str, *, from_state="", to_state="", actor=None, **data) -> AuditLog:
    return AuditLog.objects.create(
        entity_type=entity._meta.model_name,
        entity_id=entity.pk,
        action=action,
        from_state=from_state or "",
        to_state=to_state or "",
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        request_id=get_request_id() or "",
        data=data,
    )
