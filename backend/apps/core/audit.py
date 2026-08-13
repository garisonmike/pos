"""
Writing to the audit trail.

Audit entries are written by calling ``record_audit`` explicitly rather than by
hooking Django's ``post_save`` signals. Signals would catch more writes with
less code, but they cannot see the two things that make an audit trail worth
keeping: who was acting, and why. A signal fires with a model instance and no
request, so every entry would read "something changed" with no actor and no
reason - which is exactly the information a shop owner needs when stock does
not match the shelf.
"""

from __future__ import annotations

from typing import Any

from django.db import models

from apps.core.models import AuditLog
from apps.core.tenancy import get_current_tenant_id

#: Never copied into the audit trail, regardless of which model is being logged.
REDACTED_FIELDS = frozenset(
    {"password", "pin_hash", "token", "device_token", "secret", "api_key"}
)

#: Distinguishes "no tenant was supplied, use the current one" from an explicit
#: ``tenant_id=None``, which means the action belongs to the platform and to no
#: business. Without the distinction, a platform-level entry written while some
#: tenant happened to be bound would be filed against that business.
_UNSET = object()


def _redact(data: dict[str, Any] | None) -> dict[str, Any]:
    """Drop credential-shaped fields and make values JSON-safe."""
    if not data:
        return {}
    return {
        key: "[redacted]" if key in REDACTED_FIELDS else _jsonable(value)
        for key, value in data.items()
    }


def _jsonable(value: Any) -> Any:
    """Coerce model instances, UUIDs and dates into something JSONField accepts."""
    if isinstance(value, models.Model):
        return str(value.pk)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def record_audit(
    *,
    action: str,
    entity: models.Model | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor=None,
    reason: str = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request=None,
    tenant_id=_UNSET,
) -> AuditLog:
    """Record one auditable action.

    Either pass ``entity`` and let the label be derived, or pass
    ``entity_type``/``entity_id`` directly for actions that are not about a
    single row (a failed sign-in, for example).

    The tenant defaults to whichever one is bound to the current request, so
    callers cannot accidentally file an entry against the wrong business.
    Passing ``tenant_id=None`` explicitly says the action is platform-level and
    belongs to no business at all.
    """
    if entity is not None:
        entity_type = entity_type or entity._meta.label
        entity_id = entity_id or str(entity.pk)

    actor_label = ""
    if actor is not None and getattr(actor, "is_authenticated", False):
        actor_label = getattr(actor, "username", "") or str(actor.pk)

    ip_address = None
    user_agent = ""
    if request is not None:
        ip_address = _client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")[:256]

    return AuditLog.objects.create(
        tenant_id=get_current_tenant_id() if tenant_id is _UNSET else tenant_id,
        actor=actor if actor is not None and getattr(actor, "pk", None) else None,
        actor_label=actor_label,
        action=action,
        entity_type=entity_type or "",
        entity_id=str(entity_id or ""),
        reason=reason,
        before=_redact(before),
        after=_redact(after),
        ip_address=ip_address,
        user_agent=user_agent,
    )


def _client_ip(request) -> str | None:
    """Best-effort client address.

    ``X-Forwarded-For`` is trusted only for its first entry, and only because
    this is expected to sit behind a proxy that sets it. It is recorded as
    context for an investigation, never used for authorisation.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def diff_fields(
    instance: models.Model, fields: list[str]
) -> dict[str, Any]:
    """Snapshot named fields off an instance, for use as ``before``/``after``."""
    return {field: getattr(instance, field, None) for field in fields}
