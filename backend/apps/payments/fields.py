"""
Encrypted model fields.

Each tenant holds their own Daraja credentials, and those are the keys to a
business's till float. Stored in clear, a single database dump - a backup file,
a support export, a compromised read replica - would hand over every customer's
M-Pesa credentials at once.

Fernet from ``cryptography``: authenticated symmetric encryption, so a value
that has been tampered with fails to decrypt rather than decrypting to
something else.

The key comes from ``DARAJA_ENCRYPTION_KEY`` and is deliberately **not**
``SECRET_KEY``. Django's secret key is rotated for its own reasons - a leak, a
policy, a new deployment - and rotating it must not destroy every tenant's
payment configuration. Two secrets with two lifecycles.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


class EncryptionKeyMissing(ImproperlyConfigured):
    """No key configured, so credentials can be neither written nor read."""


def _fernet():
    """Build the cipher, refusing clearly rather than falling back to plaintext.

    A missing key must be an error. The tempting alternative - store it in clear
    and warn - produces a system that looks like it encrypts credentials and
    does not, which is worse than one that plainly refuses.
    """
    from cryptography.fernet import Fernet

    key = getattr(settings, "DARAJA_ENCRYPTION_KEY", "")
    if not key:
        raise EncryptionKeyMissing(
            "DARAJA_ENCRYPTION_KEY is not set, so M-Pesa credentials cannot be "
            "stored or read. Generate one with: python -c \"from "
            "cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)


class EncryptedTextField(models.TextField):
    """A text field that is ciphertext at rest and plaintext in Python.

    Encryption happens on the way to the database and decryption on the way
    back, so application code never handles a credential differently from any
    other string - which is what stops someone forgetting to decrypt and
    silently sending ciphertext to Safaricom.

    Not indexable or searchable, by design: Fernet output is randomised, so the
    same plaintext encrypts differently every time. Nothing here is ever looked
    up by value.
    """

    description = "Text, encrypted at rest"

    def get_prep_value(self, value):
        if value is None or value == "":
            return value
        if isinstance(value, bytes):
            value = value.decode()
        return _fernet().encrypt(value.encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        from cryptography.fernet import InvalidToken

        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            # Wrong key, or a tampered row. Returning the ciphertext would let a
            # broken credential travel silently into an API call to Safaricom
            # and fail there instead, far from the cause.
            raise EncryptionKeyMissing(
                "An M-Pesa credential could not be decrypted. Either "
                "DARAJA_ENCRYPTION_KEY has changed since it was stored, or the "
                "stored value has been altered."
            ) from None

    def to_python(self, value):
        if isinstance(value, bytes):
            return value.decode()
        return value
