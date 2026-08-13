"""
Root URL configuration.

Three surfaces live here, and they have different audiences:

* ``/api/v1/`` - the tenant-facing API the Flutter till talks to.
* ``/api/v1/platform/`` - cross-tenant provisioning and usage, platform
  administrators only.
* the Django admin - the platform control surface, mounted at a configurable,
  deliberately unguessable path.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from apps.core.views import HealthCheckView
from apps.platform_admin.sites import platform_admin_site

urlpatterns = [
    # The custom site, not django.contrib.admin.site: reaching it requires
    # is_platform_admin rather than merely is_staff. See platform_admin/sites.py.
    path(settings.PLATFORM_ADMIN_URL, platform_admin_site.urls),
    path("api/v1/health/", HealthCheckView.as_view(), name="health"),
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/", include("apps.tenants.urls")),
    path("api/v1/", include("apps.stores.urls")),
    path("api/v1/", include("apps.catalog.urls")),
    path("api/v1/", include("apps.inventory.urls")),
    path("api/v1/platform/", include("apps.platform_admin.urls")),
    # Schema is generated from the serializers and views themselves, so the
    # documentation cannot drift away from the implementation.
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]

if settings.DEBUG:
    # Tenant logos are served by the API only in development; a deployment puts
    # them behind the same web server that serves static files.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
