"""Routes under /api/v1/ for shifts and cash drawers."""

from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.shifts.views import ShiftViewSet

router = SimpleRouter()
router.register("shifts", ShiftViewSet, basename="shift")

urlpatterns = [path("", include(router.urls))]
