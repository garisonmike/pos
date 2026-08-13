"""Routes under /api/v1/tenant/."""

from django.urls import path

from apps.tenants.views import (
    BusinessTemplateListView,
    TenantModuleListView,
    TenantSettingsView,
    TenantSetupView,
)

urlpatterns = [
    path("tenant/", TenantSettingsView.as_view(), name="tenant-settings"),
    path("tenant/modules/", TenantModuleListView.as_view(), name="tenant-modules"),
    path("tenant/setup/", TenantSetupView.as_view(), name="tenant-setup"),
    path(
        "tenant/business-templates/",
        BusinessTemplateListView.as_view(),
        name="business-templates",
    ),
]
