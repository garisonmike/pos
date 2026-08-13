"""
Routes under /api/v1/platform/.

Tenant isolation is lifted for this whole prefix, so every route added here
must require ``IsPlatformAdmin``. The test suite asserts this rather than
trusting the reminder.
"""

from django.urls import include, path
from rest_framework.routers import SimpleRouter
from rest_framework_simplejwt.views import TokenRefreshView

from apps.platform_admin.views import (
    PlatformLoginView,
    PlatformTenantUsersView,
    PlatformTenantViewSet,
    PlatformUsageView,
)

# SimpleRouter rather than DefaultRouter: the latter adds an API root view that
# carries no permission classes of its own. Behind this prefix, where isolation
# is lifted, every route must require a platform administrator - and a test
# walks this URL conf to enforce exactly that.
router = SimpleRouter()
router.register("tenants", PlatformTenantViewSet, basename="platform-tenant")

urlpatterns = [
    path("auth/login/", PlatformLoginView.as_view(), name="platform-login"),
    # simplejwt's view unchanged: a platform administrator belongs to no
    # business, so there is no tenant to bind, and this prefix already runs
    # with isolation lifted so the user lookup succeeds.
    path("auth/refresh/", TokenRefreshView.as_view(), name="platform-token-refresh"),
    path("usage/", PlatformUsageView.as_view(), name="platform-usage"),
    path(
        "tenants/<uuid:tenant_id>/users/",
        PlatformTenantUsersView.as_view(),
        name="platform-tenant-users",
    ),
    path("", include(router.urls)),
]
