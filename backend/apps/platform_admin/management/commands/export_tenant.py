"""Create an encrypted, complete tenant data export for customer exit."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.db.models import FileField

from apps.accounts.models import User
from apps.core.models import AuditAction, AuditLog
from apps.core.tenancy import bypass_rls, tenant_context
from apps.tenants.models import Tenant

REDACTED_FIELDS = {
    "accounts.user": {"password", "pin_hash", "offline_approval_hash"},
    "accounts.device": {"token_hash"},
    "payments.mpesacredential": {
        "consumer_key",
        "consumer_secret",
        "passkey",
    },
    "payments.paymentintent": {"callback_token"},
}
TENANT_EXCLUDED_FIELDS = {"notes"}


class Command(BaseCommand):
    """Export a business's records and media as an age-encrypted ZIP stream."""

    help = "Export one tenant's business data and media encrypted to an age recipient."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", required=True, type=uuid.UUID)
        parser.add_argument(
            "--actor-username",
            required=True,
            help="Existing platform administrator initiating this export.",
        )
        parser.add_argument(
            "--recipient",
            required=True,
            help="Customer-controlled age public recipient (age1... or SSH recipient).",
        )
        parser.add_argument(
            "--output",
            required=True,
            type=Path,
            help="New encrypted output path; existing files are never overwritten.",
        )

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        actor_username = options["actor_username"].strip()
        recipient = options["recipient"].strip()
        target: Path = options["output"].expanduser().absolute()
        if not recipient:
            raise CommandError("An age public recipient is required.")
        if target.exists():
            raise CommandError("The export output already exists; choose a new path.")
        if not target.parent.is_dir():
            raise CommandError("The export output directory must already exist.")

        try:
            tenant = Tenant.objects.defer(*TENANT_EXCLUDED_FIELDS).get(pk=tenant_id)
        except Tenant.DoesNotExist as exc:
            raise CommandError("No tenant exists with that ID.") from exc
        try:
            with transaction.atomic(), bypass_rls():
                actor = User.all_objects.get(
                    username=actor_username,
                    tenant__isnull=True,
                    is_platform_admin=True,
                )
        except User.DoesNotExist as exc:
            raise CommandError(
                "The actor must be an existing platform administrator."
            ) from exc
        self._validate_recipient(recipient)

        temp_path = None
        process = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".partial", dir=target.parent
            )
            temp_path = Path(temp_name)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as encrypted_output:
                process = subprocess.Popen(
                    ["age", "--encrypt", "--recipient", recipient],
                    stdin=subprocess.PIPE,
                    stdout=encrypted_output,
                    stderr=subprocess.PIPE,
                )
                assert process.stdin is not None
                self._write_archive(process.stdin, process, tenant)
                process.stdin.close()
                error_output = process.stderr.read() if process.stderr else b""
                result = process.wait()
                if result:
                    message = error_output.decode("utf-8", errors="replace").strip()
                    raise CommandError(
                        "age could not encrypt the tenant export"
                        + (f": {message}" if message else ".")
                    )
                encrypted_output.flush()
                os.fsync(encrypted_output.fileno())

            # Linking publishes the completed artifact atomically without
            # replacing a file created concurrently by another operator.
            os.link(temp_path, target)
            try:
                self._record_export(tenant, actor, target)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            temp_path.unlink()
            self.stderr.write(
                f"Encrypted tenant export created at {target} "
                f"({tenant.slug}, {tenant.id})."
            )
        except FileNotFoundError as exc:
            if exc.filename == "age" or "age" in str(exc):
                raise CommandError(
                    "age is required for tenant exports; install it in the runtime image."
                ) from exc
            raise
        except BrokenPipeError as exc:
            detail = b""
            if process is not None:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                detail = process.stderr.read() if process.stderr else b""
                process.wait()
            message = detail.decode("utf-8", errors="replace").strip()
            raise CommandError(
                "Tenant export encryption failed"
                + (f": {message}" if message else ".")
            ) from exc
        except FileExistsError as exc:
            raise CommandError(
                "The export output already exists; choose a new path."
            ) from exc
        finally:
            if process is not None and process.poll() is None:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                process.kill()
                process.wait()
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _write_archive(self, pipe: BinaryIO, process, tenant: Tenant) -> None:
        media_manifest = []
        exported_models = []
        redacted = {"tenants.tenant": sorted(TENANT_EXCLUDED_FIELDS)}
        snapshot_tenant = tenant
        with zipfile.ZipFile(
            pipe, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                with tenant_context(tenant.id):
                    snapshot_tenant = Tenant.objects.defer(*TENANT_EXCLUDED_FIELDS).get(
                        pk=tenant.pk
                    )
                    self._write_model(
                        archive,
                        "tenants.tenant",
                        [snapshot_tenant],
                        excluded_fields=TENANT_EXCLUDED_FIELDS,
                        media_manifest=media_manifest,
                    )
                    for model in self._tenant_models():
                        label = model._meta.label_lower
                        excluded_fields = REDACTED_FIELDS.get(label, set())
                        queryset = model._base_manager.filter(
                            tenant_id=tenant.id
                        ).defer(*excluded_fields).order_by("pk")
                        self._write_model(
                            archive,
                            label,
                            queryset,
                            excluded_fields=excluded_fields,
                            media_manifest=media_manifest,
                        )
                        exported_models.append(label)
                        if REDACTED_FIELDS.get(label):
                            redacted[label] = sorted(REDACTED_FIELDS[label])

            manifest = {
                "format": "pos-tenant-export",
                "format_version": 1,
                "tenant_id": str(snapshot_tenant.id),
                "tenant_slug": snapshot_tenant.slug,
                "exported_at": datetime.now(UTC),
                "models": ["tenants.tenant", *exported_models],
                "redacted_fields": redacted,
                "media_files": media_manifest,
                "encrypted_with": "age",
            }
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, cls=DjangoJSONEncoder, indent=2).encode(),
            )

        # A failed age process may have closed its input while ZIP was writing.
        if process.poll() is not None and process.returncode != 0:
            raise BrokenPipeError("age exited before the archive was complete")

    @staticmethod
    def _tenant_models():
        models = []
        for model in apps.get_models():
            try:
                tenant_field = model._meta.get_field("tenant")
            except FieldDoesNotExist:
                continue
            if tenant_field.related_model is Tenant:
                models.append(model)
        return sorted(models, key=lambda model: model._meta.label_lower)

    @staticmethod
    def _write_model(
        archive,
        label: str,
        instances,
        *,
        excluded_fields: set[str],
        media_manifest: list[dict],
    ) -> None:
        safe_label = label.replace(".", "/")
        def iterate_instances():
            if hasattr(instances, "iterator"):
                return instances.iterator(chunk_size=500)
            return iter(instances)

        # ZIP permits one active member writer at a time. Copy files in a first
        # database pass, then stream JSONL in a second pass; this avoids keeping
        # every media-bearing model instance in memory for large tenants.
        for instance in iterate_instances():
            for field in instance._meta.concrete_fields:
                if not isinstance(field, FileField):
                    continue
                value = getattr(instance, field.attname)
                storage_name = value.name if value else ""
                if not storage_name:
                    continue
                archive_name = Command._copy_media(
                    archive, instance, field, storage_name
                )
                media_manifest.append(
                    {
                        "model": label,
                        "record_id": str(instance.pk),
                        "field": field.name,
                        "storage_name": storage_name,
                        "archive_path": archive_name,
                    }
                )

        with archive.open(f"records/{safe_label}.jsonl", "w") as target:
            for instance in iterate_instances():
                fields = {}
                for field in instance._meta.concrete_fields:
                    if field.name in excluded_fields:
                        continue
                    value = getattr(instance, field.attname)
                    if isinstance(field, FileField):
                        value = value.name if value else ""
                    fields[field.name] = value

                # Preserve M2M membership IDs without exporting shared auth or
                # permission records, which are not tenant-owned business data.
                for field in instance._meta.many_to_many:
                    fields[field.name] = list(
                        getattr(instance, field.name).values_list("pk", flat=True)
                    )

                record = {
                    "model": label,
                    "id": str(instance.pk),
                    "fields": fields,
                }
                line = json.dumps(
                    record,
                    cls=DjangoJSONEncoder,
                    separators=(",", ":"),
                )
                target.write((line + "\n").encode("utf-8"))

    @staticmethod
    def _copy_media(archive, instance, field, storage_name):
        source = field.storage.open(storage_name, "rb")
        try:
            digest = hashlib.sha256(storage_name.encode("utf-8")).hexdigest()[:20]
            safe_id = str(instance.pk).replace("/", "_").replace("\\", "_")
            archive_name = (
                f"media/{instance._meta.label_lower.replace('.', '/')}/"
                f"{safe_id}/{field.name}-{digest}"
            )
            with source, archive.open(archive_name, "w") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            return archive_name
        except Exception:
            source.close()
            raise

    @staticmethod
    def _record_export(tenant: Tenant, actor: User, artifact: Path) -> None:
        digest = hashlib.sha256()
        with artifact.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)

        with transaction.atomic(), tenant_context(tenant.id):
            AuditLog.objects.create(
                tenant=tenant,
                actor=actor,
                actor_label=actor.get_full_name() or actor.username,
                action=AuditAction.DATA_EXPORT,
                entity_type=Tenant._meta.label,
                entity_id=str(tenant.id),
                after={
                    "artifact_sha256": digest.hexdigest(),
                    "format": "pos-tenant-export-v1",
                },
            )

    @staticmethod
    def _validate_recipient(recipient: str) -> None:
        try:
            result = subprocess.run(
                ["age", "--encrypt", "--recipient", recipient],
                input=b"",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CommandError(
                "age is required for tenant exports; install it in the runtime image."
            ) from exc
        if result.returncode:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise CommandError(
                "The age public recipient is invalid"
                + (f": {message}" if message else ".")
            )
