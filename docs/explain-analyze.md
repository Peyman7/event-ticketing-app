# EXPLAIN ANALYZE findings

How to reproduce:

```bash
make seed ARGS="--events 20 --capacity 5000"   # ~290k orders, ~180k tickets, ~70k payments
make explain                                    # EXPLAIN (ANALYZE, BUFFERS), median of 7 runs
```

`manage.py explain_queries` runs each statement 7 times inside a rolled-back
transaction (EXPLAIN ANALYZE really executes UPDATEs and `FOR UPDATE`), prints the plan
of the median run and a timing table. It explains the **same querysets the endpoints
use** (`apps/reporting/queries.py` exposes them), not hand-copied SQL.

**Environment caveat:** Postgres 16 in Docker on a WSL2 laptop. Absolute numbers vary
by roughly ±50% between runs, so treat them as relative. The before/after comparison
below was measured back to back on the same dataset, with `VACUUM ANALYZE` before each
side.

## Dataset

| table | rows |
|---|---|
| orders_order | 292,769 (≈75% expired/cancelled history, ≈1% pending) |
| orders_ticket | 176,802 |
| payments_payment | 70,673 |
| busiest event | 13,856 orders, 8,500 tickets |

## Hot path: already fine, nothing changed

| # | query | median | plan |
|---|---|---|---|
| 1 | reserve: `SELECT … FOR UPDATE` on ticket types | 0.06–0.1 ms | PK index (Seq Scan when the table is tiny, which is correct) |
| 2 | expiry sweeper: pending orders past `expires_at` | 0.15–0.3 ms | **Index Scan on `order_pending_expiry_idx`** (partial, `WHERE status='pending'`) |
| 3 | idempotency key lookup | < 0.05 ms | unique index `(user, scope, key)` |
| 4 | buyer order history, first page | 0.1–0.15 ms | Index Scan on `(buyer, -created_at)`, no sort node |
| 5 | webhook: payment by provider id | < 0.1 ms | unique index |
| 6 | check-in conditional `UPDATE … RETURNING` | 0.2 ms | unique index on `code` |

Notes:
- **The sweeper's partial index** holds only pending orders (~1% of rows), so it stays
  tiny however much order history accumulates. The 500-row sweep batch reads 500 index
  entries. A plain index on `(status, expires_at)` would cover all 290k rows.
- **Seq Scan on `catalog_tickettype` / `core_idempotencykey`** appears because those
  tables hold 45 rows and 2 rows; the planner is right to skip the index at that size.
- **One-off 16 ms check-in** in an early run was a cold cache (`Buffers: read=3`), not
  the plan. Always compare warm runs.

## Reporting: before → after

| # | query | before | after | what changed |
|---|---|---|---|---|
| 7 | sales by ticket type (ORM) | 20.2 ms | **4.9 ms** | Nested Loop into `orders_orderitem` for every ticket → **Index Only Scan** |
| 8 | payment totals for event | 39.4 ms | **0.84 ms** | **Seq Scan over every payment** in the system → Index Only Scan, `Heap Fetches: 0` |
| 9 | hourly sales timeline (raw SQL) | 35.0 ms | **11.7 ms** | scanned all 13.8k orders of the event and joined tickets → partial index on paid orders, no ticket join |
| 10 | inventory reconciliation (ops) | 139 ms | 63 ms | still a full scan by design; gain is from the planner cost setting |

### 8. Payment totals: the one that didn't scale

```
Before:  Hash Join
           -> Seq Scan on payments_payment            rows=70673   <- every organizer's payments
           -> Bitmap Heap Scan on orders_order        rows=13856
After:   Index Only Scan using payment_succeeded_by_event_idx      Heap Fetches: 0
```

The filter column (`event_id`) lived on `orders_order`, the aggregate on
`payments_payment`. **No index on payments could narrow the scan.** I tried a partial
covering index on `payments(order_id)`, and the planner ignored it, correctly: probing
it 13.8k times is no cheaper than a hash join. Cost grew with **the whole platform's**
payment volume, not one event's.

Fix: denormalize `event_id` onto `Payment` (immutable, copied from the order at
creation) + `INDEX (event_id) INCLUDE (amount_cents, refunded_cents) WHERE
status='succeeded'`. The aggregate now reads only that event's index entries.

### 7. Sales by ticket type

```
Before:  Nested Loop Left Join
           -> Index Scan on orders_ticket (ticket_type_id)       rows=4250 loops=2
           -> Index Scan on orders_orderitem_pkey                loops=8500  <- price lookup
After:   Index Only Scan using ticket_type_status_cov_idx on orders_ticket
```

Changes:
1. `Ticket.price_cents`, an immutable snapshot of the seat's price at issuance (= its
   order item's `unit_price_cents`). This removes the join. Refund amounts use it too.
2. Covering index `(ticket_type_id, status) INCLUDE (price_cents, id)`. The `id` is
   there because Django compiles `Count("tickets")` to `COUNT(orders_ticket.id)`.
   Without it the first attempt was a plain Index Scan, visiting the heap for every row.
3. Dropped the now-redundant single-column FK index on `ticket_type_id`. The covering
   index leads with the same column, and every index on `orders_ticket` is paid on each
   seat sold.

### 9. Timeline (raw SQL)

- Partial index `orders(event_id, paid_at) WHERE paid_at IS NOT NULL`: reads 3.4k paid
  orders instead of all 13.8k orders of the event.
- Seats sold per bucket = `SUM(orderitem.quantity)`, so the query no longer joins
  `orders_ticket` at all.
- Why raw SQL here: `generate_series` emits empty buckets (hours with zero sales), and
  `date_trunc` takes a parameterized unit. In the ORM this becomes Python-side gap
  filling.

### Planner cost: `random_page_cost = 1.1`

With Postgres's default (`4`, which assumes spinning disks), the planner picked a
**Parallel Seq Scan over all 146k order items** for the timeline, even with the
partial index in place. At `1.1` (the usual SSD setting) it switched to a nested loop
of 3.4k index lookups. It's set in `docker-compose.yml`. In production this belongs in
the server config, after measuring on that hardware.

## Other findings

- **Django adds a second index to unique `CharField`s on Postgres** (`…_like`,
  `varchar_pattern_ops`) that only serves `LIKE 'prefix%'`. On `orders_ticket.code` it
  was 13 MB, the largest index on the hottest insert table. It's dropped in migration
  `orders/0005`; check-in uses the unique index. Payment and refund id columns have the
  same kind of index and are a candidate for the same treatment.
- **Reconciliation (#10) is a deliberate full scan.** It's an ops/CI invariant check,
  not a request path. At scale it would run per event (`WHERE tt.event_id = …`) or on a
  replica.
- **The backfill migrations** (add nullable → `UPDATE … FROM` → `SET NOT NULL`) took
  ~24 s on 177k tickets / 70k payments. On a large production table you'd backfill in
  batches and use `ADD CONSTRAINT … NOT VALID` + `VALIDATE`, to avoid one long lock.
- **Visibility map:** right after the backfill `UPDATE`, the covering indexes could
  not serve index-only scans until `VACUUM` ran. Autovacuum handles this in steady
  state, but it explains surprising plans immediately after a migration.

## Method notes (what not to trust)

- My first EXPLAIN of #8 still showed a Seq Scan after the fix. The cause: the explain
  command had a hand-copied queryset using the old `order__event_id` filter. That's why
  the command now imports the real querysets.
- Single runs were too noisy on this machine (the same untouched query varied 3×), so
  the command reports medians.
