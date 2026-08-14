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

#: Substrings that mark a field as credential-shaped.
#:
#: Matched as substrings rather than whole names, and this is the point rather
#: than a shortcut. The original version compared whole keys, which meant
#: ``password`` was redacted but ``consumer_secret``, ``passkey`` and
#: ``access_token`` were written to the trail in clear. That was harmless only
#: for as long as nothing stored credentials - the moment per-tenant M-Pesa keys
#: existed, a single audited write of that model would have put a live secret
#: into a table managers can read.
#:
#: Erring towards over-redaction is deliberate. A field wrongly redacted costs a
#: reader some context; a field wrongly kept costs a tenant their credentials.
REDACTED_SUBSTRINGS = frozenset(
    {
        "password",
        "passwd",
        "passkey",
        "secret",
        "token",
        "api_key",
        "apikey",
        "private_key",
        "credential",
        "pin_hash",
        "pin",
        "signature",
        "authorization",
        "consumer_key",
    }
)

#: Kept for callers that check membership directly. The matching itself now goes
#: through :func:`is_sensitive_field`.
REDACTED_FIELDS = REDACTED_SUBSTRINGS


def is_sensitive_field(name: str) -> bool:
    """Whether a field name looks like it carries a credential.

    Substring matching on a lowercased name, so ``consumer_secret``,
    ``mpesa_passkey`` and ``refresh_token`` are all caught without anyone having
    to remember to add each new spelling to a list.
    """
    lowered = name.lower()
    return any(marker in lowered for marker in REDACTED_SUBSTRINGS)

#: Distinguishes "no tenant was supplied, use the current one" from an explicit
#: ``tenant_id=None``, which means the action belongs to the platform and to no
#: business. Without the distinction, a platform-level entry written while some
#: tenant happened to be bound would be filed against that business.
_UNSET = object()


def _redact(data: dict[str, Any] | None) -> dict[str, Any]:
    """Drop credential-shaped fields and make values JSON-safe.

    Recurses into nested dictionaries and lists, because a payload logged whole
    - an API request body, say - hides its secrets one level down where a
    top-level scan would walk straight past them.
    """
    if not data:
        return {}
    return {key: _redact_value(key, value) for key, value in data.items()}


def _redact_value(key: str, value: Any) -> Any:
    if is_sensitive_field(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            inner: _redact_value(inner, nested) for inner, nested in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_value(key, entry) if not isinstance(entry, dict)
            else {inner: _redact_value(inner, nested) for inner, nested in entry.items()}
            for entry in value
        ]
    return _jsonable(value)


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
    """Best-effort client address for the audit trail."""
    from apps.core.net import client_ip

    return client_ip(request)


def diff_fields(
    instance: models.Model, fields: list[str]
) -> dict[str, Any]:
    """Snapshot named fields off an instance, for use as ``before``/``after``."""
    return {field: getattr(instance, field, None) for field in fields}
