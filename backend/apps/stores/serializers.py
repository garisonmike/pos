"""Serializers for branches."""

from __future__ import annotations

from rest_framework import serializers

from apps.stores.models import Store


class StoreSerializer(serializers.ModelSerializer):
    """A branch, as its business sees it."""

    class Meta:
        model = Store
        fields = (
            "id",
            "name",
            "code",
            "phone",
            "address",
            "is_active",
            "is_default",
            "created_at",
        )
        read_only_fields = ("id", "created_at")

    def validate_code(self, value: str) -> str:
        """Codes appear on receipts, so they are normalised to upper case."""
        return value.strip().upper()
