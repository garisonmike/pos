#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys


def main() -> None:
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover - import guard
        raise ImportError(
            "Could not import Django. Are you running this inside the api "
            "container? The documented entry point is `docker compose up`."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
