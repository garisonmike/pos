from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0009_refund_shift"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sale",
            name="discount_authorization_is_stale",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Set at sync when the version the device checked against no longer "
                    "matches the manager's current one, meaning the offline approval "
                    "code was changed or revoked while the till was offline. The sale "
                    "still lands - the goods left the shop - but it is flagged for a person."
                ),
            ),
        ),
        migrations.AddField(
            model_name="sale",
            name="discount_authorized_approval_version",
            field=models.PositiveIntegerField(
                blank=True,
                help_text=(
                    "Only for OFFLINE authorisations: which version of the manager's "
                    "independent offline approval code the device checked. Compared "
                    "against the manager's current version at sync."
                ),
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="sale",
            name="discount_authorized_pin_version",
            field=models.PositiveIntegerField(
                blank=True,
                help_text=(
                    "Legacy version recorded by earlier releases that used a sign-in "
                    "PIN for offline approvals. Retained for historical records only."
                ),
                null=True,
            ),
        ),
    ]
