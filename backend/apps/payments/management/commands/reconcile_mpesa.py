"""
Chase M-Pesa payments whose callback never arrived.

Run on a schedule - every couple of minutes is right, since the whole point is
to close the window in which a customer has paid and the shop does not know it.
There is no in-process scheduler here on purpose: cron or a systemd timer
already exists on any machine this runs on, and adding a worker to the compose
file to do something cron does would be carrying a service for no gain.

    */2 * * * * docker compose exec -T api python manage.py reconcile_mpesa

Safe to run by hand, and safe to run twice at once: every settlement goes
through the same locked path the callback uses, so a second run finds the intent
already resolved and does nothing.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.payments.reconciliation import reconcile


class Command(BaseCommand):
    help = "Ask Daraja what happened to payment attempts that went quiet."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--grace-seconds",
            type=int,
            default=None,
            help=(
                "How long past a prompt's expiry to wait before chasing it. "
                "Defaults to MPESA_RECONCILE_GRACE_SECONDS."
            ),
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Print nothing when there was nothing to do, for cron.",
        )

    def handle(self, *args, **options) -> None:
        report = reconcile(grace_seconds=options["grace_seconds"])

        if report.examined == 0 and options["quiet"]:
            return

        self.stdout.write(
            f"Examined {report.examined}: "
            f"{report.credited} credited, "
            f"{report.failed} confirmed unpaid, "
            f"{report.still_pending} still processing, "
            f"{report.unreachable} unreachable, "
            f"{report.skipped} skipped."
        )

        for note in report.notes:
            self.stdout.write(f"  {note}")

        if report.credited:
            self.stdout.write(
                self.style.WARNING(
                    "Payments credited from a status query carry a RECON- reference "
                    "rather than an M-Pesa receipt code, because the query does not "
                    "return one. The real code arrives on a late callback if one "
                    "ever comes."
                )
            )
