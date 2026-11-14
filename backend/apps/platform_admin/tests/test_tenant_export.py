"""The tenant exit artifact is encrypted, complete, and tenant-scoped."""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from io import StringIO
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import override_settings

from apps.accounts.models import Device
from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import tenant_context
from apps.payments.models import MpesaCredential

pytestmark = pytest.mark.django_db(transaction=True)


def _age_keypair(directory: Path) -> tuple[Path, str]:
    if shutil.which("age") is None or shutil.which("age-keygen") is None:
        pytest.fail("The backend image must include age and age-keygen.")
    identity = directory / "customer-identity.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    public_key = next(
        line.removeprefix("# public key: ").strip()
        for line in identity.read_text().splitlines()
        if line.startswith("# public key: ")
    )
    return identity, public_key


def test_export_is_customer_decryptable_redacted_scoped_and_includes_media(
    tmp_path, tenant_a, tenant_b, platform_admin, cashier_a, manager_a, item_a, item_b
):
    identity, public_key = _age_keypair(tmp_path)
    output = tmp_path / "tenant-export.zip.age"
    plaintext = tmp_path / "tenant-export.zip"
    media_root = tmp_path / "uploaded-media"
    credential_key = Fernet.generate_key().decode()

    tenant_a.notes = "internal operator note"
    tenant_a.save(update_fields=["notes", "updated_at"])
    with override_settings(
        MEDIA_ROOT=media_root,
        DARAJA_ENCRYPTION_KEY=credential_key,
    ):
        with transaction.atomic(), tenant_context(tenant_a.id):
            Device.issue(tenant=tenant_a, name="Front till")
            manager_a.rotate_offline_approval_code()
            manager_a.save()
            MpesaCredential.objects.create(
                tenant=tenant_a,
                shortcode="123456",
                consumer_key="customer-consumer-key",
                consumer_secret="customer-consumer-secret",
                passkey="customer-passkey",
            )
            item_a.image.save(
                "product-private.png", ContentFile(b"fixture image bytes"), save=True
            )
        call_command(
            "export_tenant",
            tenant_id=tenant_a.id,
            actor_username=platform_admin.username,
            recipient=public_key,
            output=output,
            stdout=StringIO(),
            stderr=StringIO(),
        )

    assert output.exists()
    assert output.read_bytes()[:2] != b"PK"
    subprocess.run(
        ["age", "--decrypt", "--identity", str(identity), "--output", str(plaintext), str(output)],
        check=True,
        capture_output=True,
    )

    with zipfile.ZipFile(plaintext) as export:
        names = set(export.namelist())
        manifest = json.loads(export.read("manifest.json"))
        users = [
            json.loads(line)
            for line in export.read("records/accounts/user.jsonl").splitlines()
        ]
        items = [
            json.loads(line)
            for line in export.read("records/catalog/item.jsonl").splitlines()
        ]
        devices = [
            json.loads(line)
            for line in export.read("records/accounts/device.jsonl").splitlines()
        ]
        credentials = [
            json.loads(line)
            for line in export.read(
                "records/payments/mpesacredential.jsonl"
            ).splitlines()
        ]
        tenant = json.loads(
            export.read("records/tenants/tenant.jsonl").splitlines()[0]
        )
        archive_contents = [
            export.read(name).decode("utf-8", errors="replace") for name in names
        ]
        assert export.testzip() is None

    exported_cashier = next(row for row in users if row["id"] == str(cashier_a.id))
    exported_manager = next(row for row in users if row["id"] == str(manager_a.id))
    assert "password" not in exported_cashier["fields"]
    assert "pin_hash" not in exported_cashier["fields"]
    assert "offline_approval_hash" not in exported_cashier["fields"]
    assert "offline_approval_hash" not in exported_manager["fields"]
    assert "token_hash" not in devices[0]["fields"]
    assert not {
        "consumer_key",
        "consumer_secret",
        "passkey",
    }.intersection(credentials[0]["fields"])
    assert "token_hash" in manifest["redacted_fields"]["accounts.device"]
    assert "pin_hash" in manifest["redacted_fields"]["accounts.user"]
    assert "offline_approval_hash" in manifest["redacted_fields"]["accounts.user"]
    assert "callback_token" in manifest["redacted_fields"]["payments.paymentintent"]
    assert "internal operator note" not in json.dumps(tenant)
    assert any(row["fields"]["name"] == item_a.name for row in items)
    assert not any(row["fields"]["name"] == item_b.name for row in items)
    assert manifest["tenant_id"] == str(tenant_a.id)
    assert manifest["tenant_id"] != str(tenant_b.id)
    assert len(manifest["media_files"]) == 1
    assert manifest["media_files"][0]["archive_path"] in names
    archive_contents_text = json.dumps(archive_contents)
    assert "customer-consumer-key" not in archive_contents_text
    assert "customer-consumer-secret" not in archive_contents_text
    assert "customer-passkey" not in archive_contents_text

    with transaction.atomic(), tenant_context(tenant_a.id):
        audit = AuditLog.objects.get(action=AuditAction.DATA_EXPORT)
    assert audit.actor_id == platform_admin.id
    assert audit.after["format"] == "pos-tenant-export-v1"
    assert audit.after["artifact_sha256"]


def test_export_refuses_to_overwrite_an_existing_artifact(
    tmp_path, tenant_a, platform_admin
):
    output = tmp_path / "existing.age"
    output.write_bytes(b"keep this artifact")

    with pytest.raises(CommandError, match="already exists"):
        call_command(
            "export_tenant",
            tenant_id=tenant_a.id,
            actor_username=platform_admin.username,
            recipient="age1invalid",
            output=output,
        )

    assert output.read_bytes() == b"keep this artifact"


def test_export_refuses_a_tenant_user_as_the_actor(
    tmp_path, tenant_a, cashier_a
):
    with pytest.raises(CommandError, match="platform administrator"):
        call_command(
            "export_tenant",
            tenant_id=tenant_a.id,
            actor_username=cashier_a.username,
            recipient="age1invalid",
            output=tmp_path / "must-not-exist.age",
        )

    assert not (tmp_path / "must-not-exist.age").exists()


def test_export_validates_the_recipient_before_creating_the_artifact(
    tmp_path, tenant_a, platform_admin
):
    with pytest.raises(CommandError, match="recipient is invalid"):
        call_command(
            "export_tenant",
            tenant_id=tenant_a.id,
            actor_username=platform_admin.username,
            recipient="age1invalid",
            output=tmp_path / "must-not-exist.age",
        )

    assert not (tmp_path / "must-not-exist.age").exists()
