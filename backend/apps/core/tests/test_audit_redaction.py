"""
What must never reach the audit trail.

Managers can read their business's audit entries, so the trail is not a safe
place for credentials. That was true from the start but only loosely enforced:
the original check compared whole field names, so ``password`` was caught while
``consumer_secret`` and ``passkey`` were written in clear.

Nothing stored credentials at the time, so nothing leaked. Per-tenant M-Pesa
keys arrive in this milestone, and a single audited write of that model would
have put a live secret into a readable table - so the check is now substring
based, and these tests hold it there.
"""

from __future__ import annotations

import pytest
from django.db import transaction

from apps.core.audit import is_sensitive_field, record_audit
from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import tenant_context

pytestmark = pytest.mark.django_db


class TestFieldNamesTreatedAsSensitive:
    @pytest.mark.parametrize(
        "name",
        [
            "password",
            "new_password",
            "current_password",
            "pin",
            "pin_hash",
            "token",
            "device_token",
            "access_token",
            "refresh_token",
            # The M-Pesa credential fields, which is why this exists at all.
            "consumer_key",
            "consumer_secret",
            "passkey",
            "mpesa_passkey",
            "api_key",
            "apikey",
            "private_key",
            "credential",
            "signature",
            "authorization",
        ],
    )
    def test_credential_shaped_names_are_caught(self, name):
        assert is_sensitive_field(name) is True

    @pytest.mark.parametrize(
        "name",
        ["price_cents", "quantity", "name", "sku", "role", "is_active", "shortcode"],
    )
    def test_ordinary_names_are_left_alone(self, name):
        """Over-redaction costs context; under-redaction costs a tenant's keys.

        But a trail with nothing readable in it is no use either, so ordinary
        fields must survive.
        """
        assert is_sensitive_field(name) is False

    def test_matching_ignores_case(self):
        assert is_sensitive_field("Consumer_Secret") is True
        assert is_sensitive_field("PASSKEY") is True


class TestRedactionInPractice:
    def test_a_secret_never_reaches_the_stored_entry(self, tenant_a, owner_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            record_audit(
                action=AuditAction.UPDATE,
                entity_type="payments.MpesaCredential",
                entity_id="1",
                actor=owner_a,
                after={
                    "shortcode": "174379",
                    "consumer_secret": "live-secret-value-9f2a",
                    "passkey": "live-passkey-value-7b31",
                },
            )
            entry = AuditLog.objects.first()

        assert entry.after["shortcode"] == "174379"
        assert entry.after["consumer_secret"] == "[redacted]"
        assert entry.after["passkey"] == "[redacted]"
        assert "live-secret-value-9f2a" not in str(entry.after)
        assert "live-passkey-value-7b31" not in str(entry.after)

    def test_a_secret_nested_one_level_down_is_still_caught(self, tenant_a, owner_a):
        """A whole payload logged as one field hides its secrets inside it.

        A top-level scan would walk straight past this, which is the shape a
        logged API request body actually takes.
        """
        with transaction.atomic(), tenant_context(tenant_a.id):
            record_audit(
                action=AuditAction.UPDATE,
                entity_type="payments.MpesaCredential",
                entity_id="1",
                actor=owner_a,
                after={
                    "request": {
                        "shortcode": "174379",
                        "consumer_secret": "nested-secret-4c81",
                    }
                },
            )
            entry = AuditLog.objects.first()

        assert entry.after["request"]["consumer_secret"] == "[redacted]"
        assert "nested-secret-4c81" not in str(entry.after)

    def test_a_secret_inside_a_list_is_caught(self, tenant_a, owner_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            record_audit(
                action=AuditAction.UPDATE,
                entity_type="payments.MpesaCredential",
                entity_id="1",
                actor=owner_a,
                after={"attempts": [{"passkey": "listed-secret-2f90", "ok": True}]},
            )
            entry = AuditLog.objects.first()

        assert "listed-secret-2f90" not in str(entry.after)

    def test_before_and_after_are_both_redacted(self, tenant_a, owner_a):
        """A change to a credential names the old value as well as the new."""
        with transaction.atomic(), tenant_context(tenant_a.id):
            record_audit(
                action=AuditAction.UPDATE,
                entity_type="payments.MpesaCredential",
                entity_id="1",
                actor=owner_a,
                before={"consumer_secret": "old-secret-1111"},
                after={"consumer_secret": "new-secret-2222"},
            )
            entry = AuditLog.objects.first()

        assert entry.before["consumer_secret"] == "[redacted]"
        assert entry.after["consumer_secret"] == "[redacted]"

    def test_ordinary_values_survive_so_the_trail_stays_useful(self, tenant_a, owner_a):
        with transaction.atomic(), tenant_context(tenant_a.id):
            record_audit(
                action=AuditAction.UPDATE,
                entity_type="catalog.Item",
                entity_id="1",
                actor=owner_a,
                before={"price_cents": 18000},
                after={"price_cents": 19500},
            )
            entry = AuditLog.objects.first()

        assert entry.before["price_cents"] == 18000
        assert entry.after["price_cents"] == 19500
