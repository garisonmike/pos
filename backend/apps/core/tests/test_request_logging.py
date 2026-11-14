import logging

from django.http import HttpResponse
from django.test import RequestFactory

from apps.core.middleware import RequestLogMiddleware


def test_mpesa_callback_token_is_redacted_from_request_logs(caplog):
    secret = "callback-token-that-must-not-be-logged"
    request = RequestFactory().post(
        f"/api/v1/payments/mpesa/callback/{secret}/?private=query-value"
    )

    with caplog.at_level(logging.INFO, logger="apps.core.middleware"):
        response = RequestLogMiddleware(lambda _: HttpResponse("ok"))(request)

    record = next(
        item for item in caplog.records if item.getMessage() == "http_request_completed"
    )
    assert response["X-Request-ID"] == record.request_id
    assert record.path == "/api/v1/payments/mpesa/callback/[redacted]/"
    assert secret not in caplog.text
    assert "private=query-value" not in caplog.text


def test_regular_request_path_remains_available_for_diagnostics(caplog):
    request = RequestFactory().get("/api/v1/health/")

    with caplog.at_level(logging.INFO, logger="apps.core.middleware"):
        RequestLogMiddleware(lambda _: HttpResponse("ok"))(request)

    record = next(
        item for item in caplog.records if item.getMessage() == "http_request_completed"
    )
    assert record.path == "/api/v1/health/"
