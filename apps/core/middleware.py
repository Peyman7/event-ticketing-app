import logging
import re
import time

from .context import new_request_id, order_id_var, request_id_var

logger = logging.getLogger("apps.request")

# Accept a caller-supplied id (e.g. from a load balancer) only if it looks sane.
_VALID_RID = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get("X-Request-ID", "")
        rid = incoming if _VALID_RID.match(incoming) else new_request_id()
        rid_token = request_id_var.set(rid)
        oid_token = order_id_var.set(None)
        request.request_id = rid
        start = time.perf_counter()
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = rid
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                },
            )
            return response
        finally:
            request_id_var.reset(rid_token)
            order_id_var.reset(oid_token)
