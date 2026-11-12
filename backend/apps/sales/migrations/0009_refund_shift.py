import django.db.models.deletion
from django.db import migrations, models


def attribute_unambiguous_refunds(apps, schema_editor):
    """Attribute only cash refunds issued inside one identifiable shift."""
    Refund = apps.get_model("sales", "Refund")
    Shift = apps.get_model("shifts", "Shift")
    Sale = apps.get_model("sales", "Sale")
    db = schema_editor.connection.alias

    # Migration code runs outside request tenant binding. Lift RLS only for
    # this transaction; this is a schema/data migration over every tenant.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.bypass_rls', 'on', true)")

    for refund in Refund.objects.using(db).filter(
        shift__isnull=True, method="CASH"
    ).iterator():
        sale = Sale.objects.using(db).filter(pk=refund.sale_id).values(
            "tenant_id", "store_id"
        ).first()
        if sale is None:
            continue

        # Original payment attribution cannot prove which drawer paid a later
        # refund. Use the refund timestamp and its sale's store instead, and
        # leave the record unassigned when the historical shift window is
        # missing or ambiguous.
        candidates = Shift.objects.using(db).filter(
            tenant_id=sale["tenant_id"],
            store_id=sale["store_id"],
            opened_at__lte=refund.created_at,
        ).filter(models.Q(closed_at__isnull=True) | models.Q(closed_at__gte=refund.created_at))
        shift_ids = list(candidates.values_list("pk", flat=True)[:2])
        if len(shift_ids) == 1:
            Refund.objects.using(db).filter(pk=refund.pk).update(shift_id=shift_ids[0])


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0008_alter_salediscrepancy_kind"),
        ("shifts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="refund",
            name="shift",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "The drawer that issued a cash refund. Null for refunds outside a "
                    "shift-managed cash drawer, including M-Pesa refunds."
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="refunds",
                to="shifts.shift",
            ),
        ),
        migrations.RunPython(attribute_unambiguous_refunds, migrations.RunPython.noop),
    ]
