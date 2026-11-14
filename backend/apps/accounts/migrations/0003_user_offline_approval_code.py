from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_user_pin_version"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="offline_approval_hash",
            field=models.CharField(
                blank=True,
                help_text=(
                    "SHA-256 digest of a high-entropy, offline-only approval code. "
                    "Never used for till sign-in."
                ),
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="offline_approval_version",
            field=models.PositiveIntegerField(
                default=0,
                help_text=(
                    "Incremented whenever the independent offline approval code is "
                    "rotated or revoked, so disconnected approvals can be flagged at sync."
                ),
            ),
        ),
    ]
