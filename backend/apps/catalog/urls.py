"""Routes under /api/v1/ for the catalogue."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.catalog.views import CategoryViewSet, ItemViewSet, TaxRateViewSet

router = DefaultRouter()
router.register("categories", CategoryViewSet, basename="category")
router.register("tax-rates", TaxRateViewSet, basename="tax-rate")
router.register("items", ItemViewSet, basename="item")

urlpatterns = [path("", include(router.urls))]
