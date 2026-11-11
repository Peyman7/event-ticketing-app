"""Optional error reporting. Off unless SENTRY_DSN is set."""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def init_error_reporting() -> bool:
    if not settings.SENTRY_DSN:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.SENTRY_ENVIRONMENT,
        integrations=[DjangoIntegration()],
        traces_sample_rate=0.0,
        send_default_pii=False,
    )
    logger.info("error reporting enabled")
    return True


def capture_exception(exc: BaseException) -> None:
    """Hook for handled-but-noteworthy errors (e.g. webhook processing failures)."""
    logger.exception("captured exception", exc_info=exc)
    if settings.SENTRY_DSN:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
