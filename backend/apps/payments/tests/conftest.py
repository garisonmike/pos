"""Fixtures for the M-Pesa tests."""

from __future__ import annotations

import pytest
from django.db import transaction

from apps.core.tenancy import tenant_context
from apps.payments.models import DarajaEnvironment, MpesaCredential
from apps.payments.testing import FakeDaraja


@pytest.fixture
def fake_daraja(settings):
    """Install a Daraja that answers however the test needs.

    Chosen by a setting rather than by sniffing the environment, so a test can
    swap it without pretending to be a different deployment.
    """
    fake = FakeDaraja()
    settings.DARAJA_CLIENT_FACTORY = lambda credential: fake
    settings.MPESA_CALLBACK_BASE_URL = "https://pos.example.com"
    return fake


@pytest.fixture
def sandbox_credential(tenant_a):
    """Sandbox credentials, where the IP allowlist does not apply."""
    with transaction.atomic(), tenant_context(tenant_a.id):
        return MpesaCredential.objects.create(
            tenant=tenant_a,
            shortcode="174379",
            consumer_key="sandbox-key",
            consumer_secret="sandbox-secret",
            passkey="sandbox-passkey",
            environment=DarajaEnvironment.SANDBOX,
        )


@pytest.fixture
def production_credential(tenant_a):
    """Production credentials, where callbacks must come from a known address."""
    with transaction.atomic(), tenant_context(tenant_a.id):
        return MpesaCredential.objects.create(
            tenant=tenant_a,
            shortcode="500123",
            consumer_key="live-key",
            consumer_secret="live-secret",
            passkey="live-passkey",
            environment=DarajaEnvironment.PRODUCTION,
            allowed_callback_ips=["196.201.214.200"],
        )
