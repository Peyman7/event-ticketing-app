"""Map domain exceptions to stable HTTP errors with machine-readable codes."""

from rest_framework import status
from rest_framework.exceptions import APIException


class DomainError(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"

    def __init__(self, code: str, detail: str, status_code: int | None = None, **extra):
        if status_code:
            self.status_code = status_code
        self.detail = {"code": code, "detail": detail, **extra}
