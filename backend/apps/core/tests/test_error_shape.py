"""What a refusal actually looks like to whoever reads it.

Written after a cashier was shown this, on a real handset, for typing the
wrong password::

    [ErrorDetail(string='Those sign-in details were not recognised.',
     code='invalid')]

A serializer normalises its errors into lists, so ``detail`` is
``[ErrorDetail(...)]`` rather than the string it resembles, and the handler
called ``str()`` on it. Every refusal originating in a serializer rendered that
way - which is every sign-in failure, the most-seen error in the product.

It survived the whole backend suite because the tests asserted refusals were
*indistinguishable from each other* and never that either was readable. Two
identically unreadable messages passed.

Mutation: revert ``_readable`` to a bare ``str()``. Every test here fails.
"""

from __future__ import annotations

import pytest

LOGIN = "/api/v1/auth/login/"


def refuse(client, slug="mama-njeri", username="nobody", password="wrong"):
    return client.post(
        LOGIN,
        {"tenant_slug": slug, "username": username, "password": password},
        format="json",
    )


@pytest.mark.django_db
class TestARefusalReadsLikeASentence:
    def test_a_wrong_password_says_so_plainly(self, anon_client, tenant_a, cashier_a):
        detail = refuse(anon_client, slug=tenant_a.slug, username="mary").json()["detail"]

        assert detail == "Those sign-in details were not recognised."

    def test_an_unknown_business_says_so_plainly(self, anon_client):
        detail = refuse(anon_client, slug="no-such-shop-anywhere").json()["detail"]

        assert detail == "Those sign-in details were not recognised."

    @pytest.mark.parametrize(
        "leak", ["ErrorDetail", "string=", "code=", "[", "]", "OrderedDict"]
    )
    def test_no_python_repr_reaches_the_till(
        self, anon_client, tenant_a, cashier_a, leak
    ):
        """The screen is read by somebody serving a customer, not by a developer."""
        detail = refuse(anon_client, slug=tenant_a.slug, username="mary").json()["detail"]

        assert leak not in detail, f"{leak!r} leaked into a user-facing message"


@pytest.mark.django_db
class TestBeingReadableDidNotMakeThemDistinguishable:
    """The property the original tests were protecting, still held.

    Both refusals had to stay byte-identical - that is what stops the endpoint
    being used to work out which business slugs are real. Making them readable
    had to keep them identical, not trade one for the other.
    """

    def test_an_unknown_business_and_a_wrong_password_still_match(
        self, anon_client, tenant_a, cashier_a
    ):
        unknown = refuse(anon_client, slug="no-such-shop-anywhere")
        wrong = refuse(anon_client, slug=tenant_a.slug, username="mary")

        assert unknown.status_code == wrong.status_code == 400
        assert unknown.json() == wrong.json()
