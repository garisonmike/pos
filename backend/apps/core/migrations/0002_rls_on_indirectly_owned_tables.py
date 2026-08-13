"""
Close the isolation gap on tables that belong to a business indirectly.

Milestone 1 protected every table carrying a ``tenant_id``. That left five
tables unprotected, all of them created by Django or a third-party app, all of
them holding rows that belong to exactly one business by way of a foreign key
to the user table.

Four were harmless in practice and one was not:

``token_blacklist_outstandingtoken``
    Stores the encoded refresh token itself, in a text column, next to the id
    of the user it was issued to. Without a policy, a query made while one
    business was bound could read another business's refresh tokens verbatim.
    This is the one that mattered.

``token_blacklist_blacklistedtoken``
    Points at the row above, so it inherits its visibility.

``accounts_user_groups`` and ``accounts_user_user_permissions``
    The join tables behind PermissionsMixin. Empty in practice - tenant users
    are authorised by their role field, not Django groups - but "empty today"
    is not a guarantee anyone should rely on.

``django_admin_log``
    Written by the platform console. Its rows reference platform administrators,
    who have no business, so they are visible only where isolation is lifted -
    which is exactly where the console runs.

The policies here are defined by the parent's visibility rather than by copying
the tenant rule, so they stay correct if that rule ever changes.
"""

from django.db import migrations

from apps.core.db.rls import enable_rls_via


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
        ("accounts", "0001_initial"),
        ("admin", "0003_logentry_add_action_flag_choices"),
        ("token_blacklist", "0012_alter_outstandingtoken_user"),
    ]

    operations = [
        enable_rls_via("accounts_user_groups", "user_id", "accounts_user"),
        enable_rls_via("accounts_user_user_permissions", "user_id", "accounts_user"),
        enable_rls_via("django_admin_log", "user_id", "accounts_user"),
        enable_rls_via("token_blacklist_outstandingtoken", "user_id", "accounts_user"),
        # Visibility follows the outstanding token, which in turn follows its
        # user, so a blacklisted token is visible exactly where the token it
        # refers to is.
        enable_rls_via(
            "token_blacklist_blacklistedtoken",
            "token_id",
            "token_blacklist_outstandingtoken",
        ),
    ]
