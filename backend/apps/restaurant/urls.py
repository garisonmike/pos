"""Routes under /api/v1/ for the restaurant module."""

from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.restaurant.views import (
    KitchenTicketViewSet,
    ModifierGroupViewSet,
    OrderViewSet,
    TableViewSet,
)

router = SimpleRouter()
router.register("tables", TableViewSet, basename="table")
router.register("orders", OrderViewSet, basename="order")
router.register("modifier-groups", ModifierGroupViewSet, basename="modifier-group")
router.register("kitchen-tickets", KitchenTicketViewSet, basename="kitchen-ticket")

urlpatterns = [path("restaurant/", include(router.urls))]
