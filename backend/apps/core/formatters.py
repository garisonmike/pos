"""Small JSON formatter for request completion events."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime


class RequestJsonFormatter(logging.Formatter):
    """Render request fields as JSON while keeping unrelated logs readable."""

    def format(self, record: logging.LogRecord) -> str:
        if not getattr(record, "request_event", False):
            return super().format(record)

        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "request_id": record.request_id,
            "method": record.method,
            "path": record.path,
            "status_code": record.status_code,
            "tenant_id": record.tenant_id,
            "duration_ms": record.duration_ms,
        }
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
