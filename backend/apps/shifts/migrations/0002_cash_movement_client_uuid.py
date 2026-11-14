import django.db.models
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("shifts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="cashmovement",
            name="client_uuid",
            field=models.UUIDField(
                blank=True,
                help_text="Till-generated idempotency key for retry-safe cash movement recording.",
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="cashmovement",
            constraint=models.UniqueConstraint(
                condition=django.db.models.Q(client_uuid__isnull=False),
                fields=("tenant", "client_uuid"),
                name="cash_movement_tenant_client_uuid_unique",
            ),
        ),
    ]
