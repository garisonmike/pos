"""
The wire shapes for compliance settings and exports.

The mode is the sensitive field here. Everything else on this surface is
presentation; changing the mode changes whether a business is declaring tax at
all, so it is validated against the adapters that actually exist rather than
against a free-text field somebody could drift.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.compliance.models import ComplianceDocument, ComplianceMode, validate_kra_pin


class ComplianceSettingsSerializer(serializers.Serializer):
    """What a business's tax setup looks like, and what may be changed."""

    compliance_mode = serializers.ChoiceField(
        choices=ComplianceMode.choices,
        required=False,
        help_text=(
            "Which regime this business files under. Owner-only: a wrong value "
            "means either declaring tax that is not owed or failing to declare "
            "tax that is."
        ),
    )
    kra_pin = serializers.CharField(
        max_length=20,
        required=False,
        allow_blank=True,
        validators=[validate_kra_pin],
        help_text="The business's own tax PIN. Shape-checked only.",
    )
    invoice_prefix = serializers.CharField(
        max_length=12,
        required=False,
        help_text="Printed before the number, e.g. TI-2026-000001.",
    )

    # Read-only, so a settings screen can show where the series has reached
    # without anybody being able to move it. A counter that could be set by
    # hand is not a gapless series.
    next_invoice_number = serializers.IntegerField(read_only=True)
    mode_label = serializers.CharField(read_only=True)

    def validate_invoice_prefix(self, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned:
            raise serializers.ValidationError("A prefix cannot be empty.")
        if not cleaned.replace("-", "").isalnum():
            raise serializers.ValidationError(
                "A prefix should be letters and digits, e.g. TI."
            )
        return cleaned


class ComplianceDocumentSerializer(serializers.ModelSerializer):
    """A document as the back office reads it."""

    is_numbered = serializers.BooleanField(read_only=True)
    receipt_code = serializers.CharField(source="sale.receipt_code", read_only=True)

    class Meta:
        model = ComplianceDocument
        fields = (
            "id",
            "kind",
            "invoice_number",
            "invoice_code",
            "is_numbered",
            "receipt_code",
            "buyer_pin",
            "seller_pin",
            "net_cents",
            "tax_cents",
            "gross_cents",
            "tax_breakdown",
            "issued_at",
            "reason",
            "original",
            "adapter",
            "submission_state",
            "submitted_at",
            "submission_reference",
            "submission_attempts",
            "failure_detail",
        )
        read_only_fields = fields
