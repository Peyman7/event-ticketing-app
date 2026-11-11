import pytest
from rest_framework.test import APIClient


@pytest.fixture(autouse=True)
def _fast_password_hashing(settings):
    # PBKDF2 costs ~0.5s per user; races create 50 users. Test-only override.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def order_factory():
    from tests.factories import OrderFactory

    return OrderFactory
