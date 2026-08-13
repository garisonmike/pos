"""
One error shape for the whole API.

The Flutter client has to handle failures on an unreliable connection, often
while a customer is waiting at the counter. Giving it a single response shape
to parse means error handling is written once, and a new endpoint cannot
introduce a format the client has never seen::

    {
      "detail": "Human-readable summary.",
      "code": "machine_readable_code",
      "fields": {"price_cents": ["This field is required."]}
    }

``fields`` is present only for validation failures. ``code`` is what the client
branches on; ``detail`` is what it shows a cashier.
"""

from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


def api_exception_handler(exc, context) -> Response | None:
    """Normalise every API error into the shape documented above."""
    if isinstance(exc, IntegrityError):
        # A unique constraint is usually a client resending something, not a
        # bug. 409 lets the till distinguish "already recorded" from "rejected".
        logger.warning("Integrity error on %s: %s", context.get("view"), exc)
        return Response(
            {
                "detail": "That conflicts with a record that already exists.",
                "code": "conflict",
            },
            status=status.HTTP_409_CONFLICT,
        )

    response = drf_exception_handler(exc, context)

    if response is None:
        return None

    code = _code_for(exc, response.status_code)
    data = response.data

    if isinstance(data, dict) and "detail" in data and len(data) == 1:
        response.data = {"detail": str(data["detail"]), "code": code}
    elif isinstance(data, dict):
        response.data = {
            "detail": "The information sent was not valid.",
            "code": "validation_error",
            "fields": data,
        }
    elif isinstance(data, list):
        response.data = {
            "detail": data[0] if data else "The request could not be processed.",
            "code": code,
        }

    return response


def _code_for(exc, status_code: int) -> str:
    """Pick the machine-readable code the client branches on."""
    explicit = getattr(exc, "default_code", None)
    if explicit:
        return str(explicit)
    if isinstance(exc, Http404):
        return "not_found"
    if isinstance(exc, PermissionDenied):
        return "permission_denied"
    return {
        400: "bad_request",
        401: "not_authenticated",
        403: "permission_denied",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        429: "throttled",
    }.get(status_code, "error")
