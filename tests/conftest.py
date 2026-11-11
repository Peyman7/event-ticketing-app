import pytest
from rest_framework.test import APIClient


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def order_factory():
    from tests.factories import OrderFactory

    return OrderFactory
