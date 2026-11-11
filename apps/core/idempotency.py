"""Idempotency-Key handling for unsafe POST endpoints.

Protocol (per user + scope + key):
  1. Same key, same body, already completed  -> replay the stored status + body.
  2. Same key, different body                 -> 422.
  3. Same key, request still running          -> the second request's INSERT blocks on
     the unique index until the first transaction ends, then falls into case 1/2.
     So N concurrent duplicates produce exactly one side effect.
  4. 5xx / crash                              -> whole transaction rolls back, key
     included, so the client may retry.

Business-rule failures (4xx such as 409 sold out) are stored and replayed: retrying a
"sold out" with the same key must not suddenly succeed and create a different outcome.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from .models import IdempotencyKey

logger = logging.getLogger(__name__)

HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"
MAX_KEY_LEN = 255


class _RollbackWith(Exception):
    def __init__(self, response):
        self.response = response


def request_fingerprint(method: str, path: str, data) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{method}\n{path}\n{canonical}".encode()).hexdigest()


def _error(status_code: int, code: str, detail: str) -> Response:
    return Response({"code": code, "detail": detail}, status=status_code)


def idempotent(scope: str):
    """Decorator for DRF view handler methods (self, request, *args, **kwargs)."""

    def decorator(handler):
        @functools.wraps(handler)
        def wrapper(view, request, *args, **kwargs):
            key = request.headers.get(HEADER, "")
            if not key or len(key) > MAX_KEY_LEN:
                return _error(400, "idempotency_key_required", f"{HEADER} header is required.")
            fingerprint = request_fingerprint(request.method, request.path, request.data)
            try:
                with transaction.atomic():
                    try:
                        with transaction.atomic():  # savepoint: a dup must not poison the txn
                            record = IdempotencyKey.objects.create(
                                user=request.user, scope=scope, key=key, request_hash=fingerprint
                            )
                    except IntegrityError:
                        record = None
                    if record is None:
                        return _replay(request.user, scope, key, fingerprint)

                    try:
                        with transaction.atomic():  # business work; rolls back on 4xx too
                            response = handler(view, request, *args, **kwargs)
                    except APIException as exc:
                        response = view.handle_exception(exc)
                    if response.status_code >= 500:
                        raise _RollbackWith(response)
                    record.response_status = response.status_code
                    record.response_body = response.data
                    record.save(update_fields=["response_status", "response_body"])
                    return response
            except _RollbackWith as rb:
                return rb.response

        return wrapper

    return decorator


def _replay(user, scope: str, key: str, fingerprint: str) -> Response:
    record = IdempotencyKey.objects.get(user=user, scope=scope, key=key)
    if record.request_hash != fingerprint:
        return _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "idempotency_key_reused",
            "This Idempotency-Key was used with a different request body.",
        )
    logger.info("idempotent replay", extra={"scope": scope})
    return Response(
        record.response_body, status=record.response_status, headers={REPLAY_HEADER: "true"}
    )
