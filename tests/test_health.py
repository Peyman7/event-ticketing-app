import pytest


@pytest.mark.django_db
def test_health_reports_db_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok"}


@pytest.mark.django_db
def test_request_id_is_echoed_or_generated(client):
    resp = client.get("/health", HTTP_X_REQUEST_ID="abc-123")
    assert resp["X-Request-ID"] == "abc-123"
    generated = client.get("/health", HTTP_X_REQUEST_ID="bad id with spaces!")["X-Request-ID"]
    assert generated != "bad id with spaces!" and len(generated) == 32


@pytest.mark.django_db
def test_openapi_schema_renders(client):
    resp = client.get("/api/schema/")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_openapi_schema_has_no_warnings():
    from io import StringIO

    from django.core.management import call_command

    err = StringIO()
    call_command("spectacular", "--validate", "--fail-on-warn", stdout=StringIO(), stderr=err)
