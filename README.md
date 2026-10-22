# Event Booking System

Django + DRF service for creating events and booking tickets. Each event has a fixed capacity and it can't be oversold, even with lots of concurrent requests. Bookings start as PENDING and expire if they're not confirmed within 10 minutes.

## Running

```bash
docker compose up --build
docker compose exec web python manage.py createsuperuser
```

API is at `http://localhost:8000/api/`, auth is HTTP Basic.

Prometheus metrics are at `/metrics` (see [Metrics](#metrics)).

Tests run on GitHub Actions against Postgres on every push (`.github/workflows/tests.yml`).

API docs (Swagger UI) are at `http://localhost:8000/api/docs/` and the raw OpenAPI schema at `/api/schema/`.

Tests (need Postgres, SQLite doesn't support the row locks):

```bash
docker compose run --rm web python manage.py test
```

## Endpoints

```
POST /api/events/                  create event (title, description, capacity, event_date)
GET  /api/events/                  list events
GET  /api/events/{id}/             event detail + booking stats
POST /api/bookings/                book, body {"event_id": 1}, optional Idempotency-Key header
GET  /api/bookings/                my bookings
POST /api/bookings/{id}/confirm/   PENDING -> CONFIRMED (fake payment)
POST /api/bookings/{id}/cancel/    PENDING/CONFIRMED -> CANCELLED
```

Event detail returns `capacity`, `active_bookings` (pending + confirmed), `confirmed_bookings` and `remaining_capacity`.

List endpoints use cursor pagination, 20 per page. The response has `next`, `previous` and `results`; follow the `next` link to get the next page. Events are sorted by date, bookings newest first.

Booking returns 201 when a new booking is created and 200 when an existing one is returned. Rule violations (sold out, wrong status, expired) return 409. Bookings of other users return 404. An `Idempotency-Key` longer than 64 characters returns 400.

Example:

```bash
curl -u admin:pass -H 'Content-Type: application/json' -X POST localhost:8000/api/events/ \
  -d '{"title":"Gig","capacity":100,"event_date":"2030-01-01T20:00:00Z"}'

curl -u admin:pass -H 'Content-Type: application/json' -H 'Idempotency-Key: abc123' \
  -X POST localhost:8000/api/bookings/ -d '{"event_id":1}'
```

## Structure

```
config/              settings, urls, celery app
bookings/
  models.py          Event, Booking, constraints, stats query
  services.py        book / confirm / cancel / expire logic
  serializers.py
  views.py
  exceptions.py      BookingError -> 409
  tasks.py           celery task for expiring bookings
```

Views just parse the request and call `services.py`. All the rules and transactions live in services so they can be tested without going through HTTP.

Running containers: web, celery worker, celery beat, postgres, redis, plus a one-off `migrate` container. Web, worker and beat only start after migrations finish, so the sweep never runs against a missing table.

## Data model

`Event` has a check constraint `capacity > 0`.

`Booking` has event, user, status (PENDING, CONFIRMED, CANCELLED, EXPIRED), expires_at and an optional idempotency_key. Indexes/constraints:

- partial unique index on `(event, user)` where status is PENDING or CONFIRMED, so the DB itself enforces one active booking per user per event. Cancelled/expired rows don't block rebooking.
- unique `(user, idempotency_key)`
- partial index on `expires_at` where status is PENDING, used by the expiry job

I didn't add a "seats taken" counter on Event. Remaining capacity is always computed by counting active bookings, so cancel and expire don't need to update anything else and the numbers can't drift.

## Concurrency

I went with pessimistic locking. `services.book()` does this in one transaction:

1. `select_for_update()` on the event row
2. mark this event's overdue PENDING bookings as EXPIRED
3. if the user already has an active booking, return it
4. count active bookings, if it's >= capacity raise "sold out"
5. insert the new PENDING booking

Since everything after step 1 runs while holding the lock, two requests can't both see a free seat. Postgres runs in READ COMMITTED by default, so the count in step 4 sees whatever the previous lock holder committed. The lock is per event, so bookings for different events don't block each other.

Confirm and cancel only lock the booking row. They don't need the event lock because cancelling frees the seat just by changing status (it's no longer counted).

Deadlocks: book locks event then bookings, confirm/cancel lock one booking, the expiry job only touches bookings. Nothing locks in the reverse order.

`ConcurrencyTests.test_no_overselling` fires 30 threads at a 5 seat event at the same time and checks only 5 bookings exist. I also ran it with the `select_for_update` removed and it ended up with 14 to 23 bookings, so the test does catch the race.

Other options I considered:

- **Counter with conditional update** (`UPDATE event SET reserved = reserved + 1 WHERE reserved < capacity`). Shorter lock and better for one very hot event, but the counter has to be kept in sync on cancel and expire and any bug there leaks seats. This is what I'd switch to if booking throughput on a single event became a problem.
- **Optimistic locking** (version field + retry). Under high contention most requests would fail and retry, which is the exact case we care about.
- **DB constraints only**. You can't write "number of active rows <= capacity" as a normal constraint. I still use constraints as a safety net for the simpler rules.
- **SERIALIZABLE isolation**. Works, but every call has to handle serialization failures and retry.

## Idempotency

- A user can only have one active booking per event, so retrying the same booking request just returns the existing booking (200), even without a header.
- If an `Idempotency-Key` header is sent, the same key always returns the original booking, even after it's cancelled or expired. Using the same key for a different event returns 409.
- A key either returns its own booking or creates a new one. If the user already has an active booking for the event made without this key, a request with a new key gets 409 instead of the existing booking. Otherwise the key would never get saved, and replaying it after a cancel would create a second booking.
- Confirm on an already confirmed booking and cancel on an already cancelled one just return the booking instead of failing, so client retries are safe.

## Expiration

Each booking gets `expires_at = now + 10 minutes` (`BOOKING_PENDING_TTL_SECONDS`).

The important part is that correctness doesn't depend on the worker. A PENDING booking past `expires_at` is treated as inactive everywhere: it's not counted for capacity or in the stats, it can't be confirmed, and the API already returns it as EXPIRED. So the seat is freed at exactly 10 minutes even if celery is down.

Celery beat runs `expire_pending_bookings` every 30 seconds. It expires overdue bookings in batches of 1000, each batch in its own short transaction, and keeps going until a batch comes back smaller than 1000:

```sql
UPDATE booking SET status = 'EXPIRED'
WHERE id IN (
  SELECT id FROM booking
  WHERE status = 'PENDING' AND expires_at <= now()
  LIMIT 1000 FOR UPDATE SKIP LOCKED
)
```

- batching keeps each statement short. If a big event has 50k bookings expire at the same moment, it's 50 small updates instead of one huge one holding 50k row locks
- `SKIP LOCKED` means the sweep never waits on a booking that's being confirmed or cancelled right now. It just skips it and the next run gets it if it's still pending. Two sweeps running at once also just split the work instead of blocking each other
- running it twice (or on two workers) does nothing extra, so retries are fine
- nothing is kept in memory, so if the worker restarts or is down for a while the next run picks up everything it missed
- `acks_late` is on, so a task that dies mid-run gets redelivered
- if confirm and the sweep hit the same booking at the same time, whoever gets the row lock first wins. If confirm has it, the sweep skips the row. If the sweep has it, confirm waits, then sees EXPIRED and fails. A confirmed booking is never overwritten (`test_sweep_skips_rows_locked_by_confirm` checks this)

`book()` also expires overdue rows for that event before checking, so a stale PENDING row never blocks the user from booking again.

I didn't schedule one task per booking with `apply_async(countdown=600)`. Those tasks wait in worker memory, get redelivered based on the Redis visibility timeout, and are gone if Redis is flushed. The periodic sweep doesn't have these problems, and since expiry is already checked on read, the up to 30s delay before the status changes doesn't matter.

## Metrics

`/metrics` exposes a few metrics that cover the parts most likely to break:

```
booking_requests_total{result}     created, existing (retry), sold_out, conflict
booking_lock_wait_seconds          histogram, time waiting for the event row lock
booking_transitions_total{to}      confirmed, cancelled
bookings_overdue_pending           PENDING bookings past expires_at
```

What I'd alert on:

- `bookings_overdue_pending > 0` for more than a few minutes. Between sweeps it's normal for it to be non-zero for up to 30s, but if it keeps growing the worker or beat is down. Users aren't affected in that case because expiry is also checked on read, but the statuses in the DB are stale.
- p99 of `booking_lock_wait_seconds` going up. That means requests are queueing on one event's lock, which is the signal to move to the counter approach in the scaling section.

`bookings_overdue_pending` is read from the database on every scrape instead of being reported by the worker. The sweep runs in the Celery worker, a separate process, so anything it counted wouldn't show up on the web process's `/metrics`. Reading it from the DB also checks the actual result rather than trusting the worker to report on itself. The query uses the partial `expires_at` index, so it's cheap.

Notes:

- `/metrics` has no auth. It's meant to be scraped from inside the network and blocked at the proxy, which is the usual setup.
- With multiple gunicorn workers, each process keeps its own counters. That needs `PROMETHEUS_MULTIPROC_DIR` set. Not needed with `runserver`, which is a single process.

## Assumptions

- Any logged in user can create events. In a real system this would be limited to organizers/staff.
- One booking = one ticket.
- Event date has to be in the future, and you can't book an event that already started.
- Payment is simulated by calling confirm.

## If this had to scale

- The lock is per event, so throughput grows with the number of events. For a single very popular event I'd move to the conditional counter update, or split capacity into several rows to spread the lock.
- Stats are counted with a subquery per event (using the `(event, status)` index) instead of a join + GROUP BY. With GROUP BY, Postgres counted bookings for every remaining event and then sorted them just to return one page: 22ms on 50k events vs 0.14ms with subqueries. For a very popular event the counts could also be cached for a second or two.
- Lists use cursor pagination instead of page numbers. Page numbers turn into `OFFSET`, so Postgres has to read and throw away every row before the page, which gets slow deep into a big table. A cursor is a `WHERE event_date > x` on an index, so every page costs the same. It also doesn't skip or repeat rows when new ones are inserted while someone is paging. The trade-off is no total count and no jumping to page N, which these endpoints don't need.
- Not done: rate limiting, token/JWT auth, a real payment provider with webhooks calling confirm.
