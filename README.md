# BookMyShow-scale ticketing backend - P1 and P2

MySQL 8.0+ schema, queries and a concurrency test for a movie-ticketing backend where many users compete for the same seats.

| Task | Where |
|---|---|
| P1 - entities, tables, keys, sample rows, normalization, locking strategy | `sql/01_schema.sql`, `sql/02_seed.sql`, `sql/04_locking_patterns.sql`, `docs/DESIGN.md` |
| P2 - shows at a theatre on a date, with timings | `sql/03_p2_queries.sql` |
| Concurrency proof (100+ parallel connections) | `tests/concurrency_test.py` |
| Full write-up (PDF) | `docs/BookMyShow_P1_P2.pdf` |

## Run it

```bash
mysql -u root -p < sql/01_schema.sql     # creates database `bookmyshow`
mysql -u root -p bookmyshow < sql/02_seed.sql
mysql -u root -p bookmyshow < sql/03_p2_queries.sql
mysql -u root -p bookmyshow < sql/04_locking_patterns.sql   # optional: walks through hold -> confirm -> expiry -> webhook

pip install pymysql
DB_HOST=127.0.0.1 DB_USER=root DB_PASSWORD=... python tests/concurrency_test.py
```

To change the P2 inputs, edit `@theatre_id` and `@show_date` at the top of `sql/03_p2_queries.sql`.
Seed show dates are relative to `CURDATE()`, so the next-7-days date picker always has data.

## Design in one paragraph

`show_seat` holds one row per (show, seat). Holding a seat is a single conditional `UPDATE ... WHERE status='AVAILABLE'`
inside a short transaction, so InnoDB row locks let exactly one of N racers win, and the transaction is all-or-nothing across
seats. A hold is a state with a deadline (`booking.hold_expires_at`), never an open lock; an idempotent two-step sweeper releases
expired holds. Payment webhooks are inserted first under `UNIQUE (provider, provider_event_id)`, so redeliveries are no-ops.
Redis, the payment gateway, the API and the burst queue are not part of this deliverable.

## Regenerating the docs

`python docs/build_docs.py` rebuilds the database, runs the queries and the concurrency test, and regenerates
`docs/DESIGN.md`, `docs/er_diagram.png` and the PDF (needs `pymysql`, `reportlab`, `pillow` and Graphviz `dot`).
