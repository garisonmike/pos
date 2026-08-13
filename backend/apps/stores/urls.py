"""Routes under /api/v1/."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.stores.views import StoreViewSet

router = DefaultRouter()
router.register("stores", StoreViewSet, basename="store")

urlpatterns = [path("", include(router.urls))]
