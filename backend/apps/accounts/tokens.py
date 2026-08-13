"""
Issuing access tokens.

The tenant is carried as a claim on the token because
``TenantBindingMiddleware`` has to know which business a request belongs to
before it can safely query anything - including the user table, which is itself
tenant-isolated. Reading it from a signed claim breaks that circularity without
a database round trip.

The claim decides which rows are *visible*. It never decides what the user is
allowed to *do*: authorisation reads the user record loaded from the database,
so a token cannot grant a role its owner does not actually hold.
"""

from __future__ import annotations

from rest_framework_simplejwt.tokens import RefreshToken


def build_refresh_token(user) -> RefreshToken:
    """Create a refresh token carrying the claims the platform relies on.

    Custom claims are attached to the refresh token rather than only to the
    access token, because simplejwt derives each refreshed access token from
    the refresh token's payload. Setting them here means a till that has been
    running for a fortnight keeps a correctly scoped token without re-issuing
    one from scratch.
    """
    token = RefreshToken.for_user(user)
    token["tenant_id"] = str(user.tenant_id) if user.tenant_id else None
    token["tenant_slug"] = user.tenant.slug if user.tenant_id else None
    token["role"] = user.role
    token["is_platform_admin"] = user.is_platform_admin
    token["username"] = user.username
    return token


def issue_tokens_for(user) -> dict[str, str]:
    """Return the access and refresh pair a client stores after signing in."""
    refresh = build_refresh_token(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}
