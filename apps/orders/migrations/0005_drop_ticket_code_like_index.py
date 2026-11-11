"""Drop the varchar_pattern_ops ("_like") index Django adds to unique CharFields on Postgres.

It only serves LIKE 'prefix%' queries, which we never run on ticket codes; equality
lookups (check-in) use the unique index. It was the largest index on the hottest insert
table (one row per seat sold), so it was pure write amplification during on-sales.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("orders", "0004_covering_index_include_id")]

    operations = [
        migrations.RunSQL(
            "DROP INDEX IF EXISTS orders_ticket_code_0db5ed34_like",
            reverse_sql="CREATE INDEX orders_ticket_code_0db5ed34_like "
            "ON orders_ticket (code varchar_pattern_ops)",
        )
    ]
