"""Routes under /api/v1/auth/."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.accounts.views import (
    ChangePasswordView,
    DeviceViewSet,
    LogoutView,
    MeView,
    PinLoginView,
    TenantLoginView,
    TenantTokenRefreshView,
    UserViewSet,
)

router = DefaultRouter()
router.register("users", UserViewSet, basename="user")
router.register("devices", DeviceViewSet, basename="device")

urlpatterns = [
    path("login/", TenantLoginView.as_view(), name="login"),
    path("pin-login/", PinLoginView.as_view(), name="pin-login"),
    # Not simplejwt's view directly: a refresh request carries its token in the
    # body rather than the Authorization header, so the middleware has nothing
    # to bind a tenant from. See TenantTokenRefreshView for why that matters.
    path("refresh/", TenantTokenRefreshView.as_view(), name="token-refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", MeView.as_view(), name="me"),
    path("change-password/", ChangePasswordView.as_view(), name="change-password"),
    path("", include(router.urls)),
]
