"""Endpoints that belong to no particular app."""

from django.db import connection
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthCheckView(APIView):
    """Liveness probe that also proves the database is reachable.

    The Flutter client calls this to decide whether it is online, so it checks
    the database rather than only returning 200: an API that is up but cannot
    reach Postgres would otherwise convince a till it is online and let a
    cashier start a sale that cannot be saved. Better for the device to stay in
    offline mode and queue the sale.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        summary="Service health",
        description=(
            "Returns 200 when the API and its database are both reachable, "
            "503 otherwise. Used by the till to decide between online and "
            "offline mode."
        ),
        responses={200: dict, 503: dict},
        tags=["health"],
    )
    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            return Response(
                {"status": "unavailable", "database": "down"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"status": "ok", "database": "up"})
