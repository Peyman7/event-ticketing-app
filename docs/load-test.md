# Load test: on-sale spike

## Scenario (`loadtest/locustfile.py`)

One event goes on sale with **1,000 seats** (800 GA, 200 VIP, max 4 per order).
Simulated buyers arrive at 100/s. Each one:

1. `GET /api/events/{id}/`
2. `POST /api/orders/` for 1–4 seats with a fresh `Idempotency-Key`.
   **10% re-send the same request with the same key** (a client retry after a
   timeout); the replay must return the same order id and `Idempotent-Replayed: true`.
3. `POST /api/orders/{id}/pay/` with `pm_ok`
4. Polls the order until the payment webhook marks it `paid` (reported as the
   synthetic `pay -> webhook -> paid` timing, 20 s timeout)

`409 sold_out` counts as an expected outcome, not a failure. That buyer stops.

## How to run

```bash
make up
docker compose run --rm web python manage.py loadtest_setup --buyers 2000 --ga 800 --vip 200
make loadtest USERS=300 SPAWN=100 DURATION=60s     # headless, writes loadtest/results/
docker compose run --rm web python manage.py loadtest_report       # correctness check
docker compose logs web --since 5m --no-log-prefix | python loadtest/server_latency.py
```

- `loadtest_setup` pre-creates buyers with API tokens, so the test measures checkout
  rather than PBKDF2 password hashing.
- `loadtest_report` fails (non-zero exit) on oversell, double charge, inventory
  mismatch, or a retry that created a second order.
- `server_latency.py` reads the app's own JSON request logs (`duration_ms`), so
  server-side latency can be compared with what Locust sees.
- `make loadtest` runs Locust with `--no-deps` and `--processes 4`. Locust's
  `depends_on: web` would otherwise let compose silently recreate the web container
  with different settings mid-experiment, which happened to me (see below).

## Results

**Test rig:** a WSL2 laptop. Postgres, gunicorn (4 sync workers), the Procrastinate
worker and Locust (2 processes) all share one VM with **3.7 GB RAM, whose 1 GB swap
was exhausted** by other processes on the machine. **Treat absolute latencies as a
property of this rig, not of the service.**

### Correctness (the point of the exercise)

Final run: 300 users, 60 s. `loadtest_report` was re-run afterwards against **every**
event used while investigating performance (8 runs, up to 500 users). All passed. Seven
had traffic; one (event 29) made no orders, see item 3 below. In three of the 30 s runs
the test stopped before every buyer paid. Their holds then **expired under real
conditions and released correctly** (reserved == seats in paid orders afterwards).

```
capacity                              1000
reserved_count (held + sold)           552    <= capacity
tickets_issued                         552    == seats in paid orders
orders                                 223 paid
payments                               223 succeeded, max 1 per order
checkout keys that returned 201        223 -> 223 distinct orders (29 same-key retries all replayed)
webhooks                               processed exactly once each
inventory_mismatches                    []
invariants hold
```

Earlier 500-user runs sold the VIP allocation out completely (200/200) while buyers
raced. VIP sold out first, and those buyers stopped, so total seats sold stayed
below 1,000.

### Latency: server vs client (same run)

| endpoint | server p50 | server p95 | client p50 (Locust) | client p95 |
|---|---|---|---|---|
| `GET /api/events/{id}/` | 20 ms | 35 ms | 2.3 s | 2.9 s |
| `POST /api/orders/` (checkout) | **70 ms** | **200 ms** | 3.7 s | 7.2 s |
| `POST /api/orders/{id}/pay/` | 118 ms | 271 ms | 8.6 s | 10 s |
| `POST /api/webhooks/payments/` | 18 ms | 87 ms | — | — |
| `pay -> webhook -> paid` | — | — | > 20 s (timeout) | — |

The checkout server time includes the row lock on the hot ticket-type rows, the hold,
the expiry job, the audit row and the idempotency record, all in one transaction.

## What the investigation showed

The client-side numbers were 30–100× the server-side ones, so I worked through the
gap instead of tuning blindly:

1. **More gunicorn workers (4 → 12): no change.** It turned out the 12-worker runs
   were actually 4 workers. Locust's `depends_on: web` made compose recreate the web
   container with the default setting. Fixed with `--no-deps` and `WEB_CONCURRENCY`
   wired through compose.
2. **Locust CPU (1 → 4 processes): no change.**
3. **Docker DNS** (every sync-worker request opens a new connection): **not actually
   tested.** My run against the container IP was rejected by `ALLOWED_HOSTS` (a raw IP
   isn't an allowed Host), so every request was a 400. I initially misread that as "no
   better" and caught it only when re-verifying every run. Not retested, because item 6
   explained the gap.
4. **Request logging to Docker's log pipe:** a minimal stdlib client against `/health`
   gave ~325 req/s with logging off vs ~350 with it on. Ruled out.
5. **Postgres:** `pg_stat_activity` during load showed every web connection
   `idle / ClientRead`, i.e. waiting on the app. Not the database.
6. **Memory:** `MemAvailable` ~175 MB and **swap 100% used** in the Docker VM.
   Restarting the web container alone moved `/health` from 110 to 340 req/s. This is
   the bottleneck: a co-located, memory-starved rig.

**Payment confirmation latency** is a chain of four background jobs (submit → provider
settle → webhook delivery over HTTP → process). In this setup the fake provider's
webhook POST also **queues behind buyer traffic on the same gunicorn workers**. At
idle the whole chain completes in ~2 s (verified end to end). Under load it exceeded
the 20 s poll window, while every payment still confirmed correctly once the backlog
drained.

## What I'd do with a proper rig

- Run the load generator on separate machines (Locust master + workers) and give the
  service its own hosts; then find the real knee by stepping users up until server
  p95 bends.
- Give **webhooks their own ingress / worker pool**, so provider callbacks don't
  queue behind an on-sale spike.
- Use a **virtual waiting room** in front of checkout at the real 100x point (see the
  README, "At 100x scale").
- Watch the **hot-row lock wait** on `catalog_tickettype` (`pg_stat_activity`
  wait_event `Lock:transactionid`) as the true ceiling for a single ticket type. The
  README discusses sharding inventory into buckets.
