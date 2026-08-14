"""
Routes under /api/v1/payments/.

The callback is the only unauthenticated route in the system that moves money.
It is deliberately the only path under this prefix that runs a query before a
tenant is bound, and the URL-conf test asserts that stays true.
"""

from django.urls import path

from apps.payments.views import MpesaCallbackView

urlpatterns = [
    path(
        "mpesa/callback/<str:token>/",
        MpesaCallbackView.as_view(),
        name="mpesa-callback",
    ),
]
