"""Routes under /api/v1/ for stock."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.inventory.views import StockItemViewSet

router = DefaultRouter()
router.register("stock", StockItemViewSet, basename="stock")

urlpatterns = [path("", include(router.urls))]
