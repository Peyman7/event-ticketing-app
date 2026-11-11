from rest_framework.views import exception_handler as drf_exception_handler

from .context import get_request_id


def exception_handler(exc, context):
    """DRF default handler, plus the request id so clients can quote it in bug reports."""
    response = drf_exception_handler(exc, context)
    if response is not None and isinstance(response.data, dict):
        response.data.setdefault("request_id", get_request_id())
    return response
