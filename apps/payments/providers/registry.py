from functools import cache

from django.conf import settings
from django.utils.module_loading import import_string

from .base import PaymentProvider


@cache
def get_provider() -> PaymentProvider:
    return import_string(settings.PAYMENT_PROVIDER_CLASS)()
