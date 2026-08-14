"""Routes under /api/v1/ for sales."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.sales.views import SaleViewSet

router = DefaultRouter()
router.register("sales", SaleViewSet, basename="sale")

urlpatterns = [path("", include(router.urls))]
