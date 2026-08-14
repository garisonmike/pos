from django.urls import path

from apps.sync.views import CatalogSyncView, SaleSyncView

urlpatterns = [
    path("sync/sales/", SaleSyncView.as_view(), name="sync-sales"),
    path("sync/catalog/", CatalogSyncView.as_view(), name="sync-catalog"),
]
