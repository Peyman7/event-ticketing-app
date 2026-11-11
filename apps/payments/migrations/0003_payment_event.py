"""Denormalise payment.event_id for per-event cash totals (see docs/explain-analyze.md)."""

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("payments", "0002_webhookevent_refund"), ("catalog", "0001_initial")]

    operations = [
        migrations.AddField(
            "payment",
            "event",
            models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payments",
                to="catalog.event",
            ),
        ),
        migrations.RunSQL(
            "UPDATE payments_payment p SET event_id = o.event_id "
            "FROM orders_order o WHERE o.id = p.order_id",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            "payment",
            "event",
            models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payments",
                to="catalog.event",
            ),
        ),
        migrations.AddIndex(
            "payment",
            models.Index(
                fields=["event"],
                include=["amount_cents", "refunded_cents"],
                condition=Q(status="succeeded"),
                name="payment_succeeded_by_event_idx",
            ),
        ),
    ]
