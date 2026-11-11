"""Denormalise ticket price + reporting indexes (see docs/explain-analyze.md).

Add nullable -> backfill in SQL -> set NOT NULL: safe on a table that already has rows.
"""

from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("orders", "0001_initial"), ("catalog", "0001_initial")]

    operations = [
        migrations.AddField("ticket", "price_cents", models.BigIntegerField(null=True)),
        migrations.RunSQL(
            "UPDATE orders_ticket t SET price_cents = oi.unit_price_cents "
            "FROM orders_orderitem oi WHERE oi.id = t.order_item_id",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField("ticket", "price_cents", models.BigIntegerField()),
        migrations.AddConstraint(
            "ticket",
            models.CheckConstraint(condition=Q(price_cents__gte=0), name="ticket_price_gte_0"),
        ),
        migrations.RemoveIndex("ticket", "orders_tick_ticket__cc07ad_idx"),
        migrations.AddIndex(
            "ticket",
            models.Index(
                fields=["ticket_type", "status"],
                include=["price_cents"],
                name="ticket_type_status_cov_idx",
            ),
        ),
        migrations.AddIndex(
            "order",
            models.Index(
                fields=["event", "paid_at"],
                condition=Q(paid_at__isnull=False),
                name="order_paid_by_event_idx",
            ),
        ),
    ]
