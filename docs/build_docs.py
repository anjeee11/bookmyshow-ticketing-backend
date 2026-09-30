#!/usr/bin/env python3
"""
Builds docs/er_diagram.png, docs/DESIGN.md and docs/BookMyShow_P1_P2.pdf from a LIVE MySQL
database, so every example row / query result in the doc is real output.

    python docs/build_docs.py          (needs MySQL 8, pymysql, reportlab, graphviz `dot`)
"""
import datetime
import os
import re
import subprocess
import sys

import pymysql
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, Preformatted,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL = os.path.join(ROOT, "sql")
DOCS = os.path.join(ROOT, "docs")
MYSQL = ["mysql", "-uroot"]


def sh(cmd, stdin_path=None):
    with open(stdin_path) if stdin_path else open(os.devnull) as fh:
        r = subprocess.run(cmd, stdin=fh, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"command failed: {cmd}\n{r.stderr}")
    return r.stdout


def connect():
    return pymysql.connect(host="localhost", user="root", database="bookmyshow",
                           unix_socket="/var/run/mysqld/mysqld.sock")


# ---------------------------------------------------------------------------
# 1. Rebuild DB, gather live facts
# ---------------------------------------------------------------------------
sh(MYSQL, os.path.join(SQL, "01_schema.sql"))
sh(MYSQL + ["bookmyshow"], os.path.join(SQL, "02_seed.sql"))
db = connect()
cur = db.cursor()

TABLES = ["city", "theatre", "screen", "seat_category", "seat", "language", "show_format", "movie",
          "movie_show", "show_price", "app_user", "booking", "show_seat", "booking_seat",
          "payment", "payment_webhook_event"]
HIDE_COLS = {"created_at", "updated_at", "received_at"}   # noise in the example rows

PURPOSE = {
    "city": "Cities in which theatres operate.",
    "theatre": "A cinema venue in a city (the entity the date-picker page is about).",
    "screen": "An auditorium inside a theatre; owns a fixed seat layout.",
    "seat_category": "Pricing tier of a seat (SILVER / GOLD / RECLINER).",
    "seat": "Physical seat in a screen. Exists once, independent of any show.",
    "language": "Audio language a show is played in.",
    "show_format": "Projection format (2D, 3D, IMAX 2D).",
    "movie": "A film in the catalogue.",
    "movie_show": "One screening: a movie on a screen at a date and start time. (Named movie_show because SHOW is reserved in MySQL.)",
    "show_price": "Price of each seat tier for a specific show.",
    "app_user": "A registered customer.",
    "booking": "A customer's attempt/purchase for one show. PENDING while seats are held and payment is outstanding.",
    "show_seat": "Inventory row per (show, seat). The concurrency-control table: every hold / confirm / release is a compare-and-set here.",
    "booking_seat": "Immutable line items of a booking (which seats, at what price). Kept after expiry/cancel for history.",
    "payment": "A payment attempt for a booking at a gateway.",
    "payment_webhook_event": "Inbox of gateway webhook deliveries. Its unique key is what makes webhook handling idempotent.",
}
NOTES = {
    ("movie_show", "show_date"): "Business date of the show; drives the P2 lookup",
    ("movie_show", "start_time"): "end time is derived (start + movie duration), not stored",
    ("show_seat", "status"): "AVAILABLE / HELD / BOOKED",
    ("show_seat", "booking_id"): "current holder/owner; NULL iff AVAILABLE (CHECK)",
    ("show_seat", "version"): "optimistic-lock counter, +1 on every transition",
    ("booking", "hold_expires_at"): "hold deadline; swept by idx_booking_sweeper",
    ("booking", "status"): "PENDING / CONFIRMED / CANCELLED / EXPIRED",
    ("booking_seat", "price_paid"): "price snapshot at hold time",
    ("payment", "provider_txn_id"): "gateway id; UNIQUE with provider",
    ("payment_webhook_event", "provider_event_id"): "gateway's event id; UNIQUE with provider = idempotency key",
    ("payment_webhook_event", "payload"): "raw JSON stored verbatim for audit",
}
SAMPLE_SQL = {
    "show_seat": "SELECT {cols} FROM show_seat WHERE show_id=1 AND (booking_id IS NOT NULL OR seat_id=1) ORDER BY show_seat_id",
    "booking_seat": "SELECT {cols} FROM booking_seat ORDER BY booking_id, show_seat_id",
    "movie_show": "SELECT {cols} FROM movie_show WHERE show_id IN (1,2,32,33,34,63,64,65) ORDER BY show_id",
    "show_price": "SELECT {cols} FROM show_price WHERE show_id IN (1,2) ORDER BY show_id, seat_category_id",
    "seat": "SELECT {cols} FROM seat WHERE screen_id=1 AND seat_number<=2 ORDER BY seat_id",
}


def columns_of(t):
    cur.execute("SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY, IS_NULLABLE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA='bookmyshow' AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (t,))
    return cur.fetchall()


def fks_of(t):
    cur.execute("SELECT COLUMN_NAME, REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA='bookmyshow' AND TABLE_NAME=%s AND REFERENCED_TABLE_NAME IS NOT NULL", (t,))
    return dict(cur.fetchall())


def fmt(v):
    if v is None:
        return "NULL"
    if isinstance(v, (datetime.datetime,)):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, datetime.timedelta):
        s = int(v.total_seconds())
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"
    return str(v)


table_docs = {}
for t in TABLES:
    cols = columns_of(t)
    fks = fks_of(t)
    attr_rows = []
    for name, ctype, key, nullable in cols:
        flags = []
        if key == "PRI":
            flags.append("PK")
        if name in fks:
            flags.append(f"FK -> {fks[name]}")
        if key == "UNI" and "PK" not in flags:
            flags.append("UNIQUE")
        if nullable == "YES":
            flags.append("NULL")
        note = NOTES.get((t, name), "")
        attr_rows.append([name, ctype, ", ".join(flags), note])
    shown = [c[0] for c in cols if c[0] not in HIDE_COLS]
    q = SAMPLE_SQL.get(t, "SELECT {cols} FROM " + t + " ORDER BY 1 LIMIT 4").format(cols=", ".join(shown))
    cur.execute(q)
    sample = [[fmt(v) for v in row] for row in cur.fetchall()]
    table_docs[t] = dict(attrs=attr_rows, sample_cols=shown, sample=sample)

# P2 output (as the mysql client prints it) + EXPLAIN as rows
p2_out = sh(MYSQL + ["-t"], os.path.join(SQL, "03_p2_queries.sql"))
p2_blocks = [b for b in p2_out.strip().split("\n\n")] if "\n\n" in p2_out else [p2_out]
cur.execute("SET @theatre_id=1, @show_date=CURDATE()")
cur.execute("""EXPLAIN SELECT s.show_id, m.title, l.name, f.name, sc.name, s.start_time
FROM screen sc JOIN movie_show s ON s.screen_id=sc.screen_id JOIN movie m ON m.movie_id=s.movie_id
JOIN language l ON l.language_id=s.language_id JOIN show_format f ON f.format_id=s.format_id
WHERE sc.theatre_id=@theatre_id AND s.show_date=@show_date AND s.status='SCHEDULED'
ORDER BY m.title, s.start_time""")
explain_rows = [[fmt(r[2]), fmt(r[4]), fmt(r[6]), fmt(r[9]), fmt(r[11])] for r in cur.fetchall()]  # table,type,key,rows,Extra
db.close()

# concurrency test (runs against the same DB; cleans up after itself)
test_out = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "concurrency_test.py")],
                          capture_output=True, text=True)
if test_out.returncode:
    sys.exit("concurrency tests failed:\n" + test_out.stdout + test_out.stderr)
test_text = test_out.stdout.strip()
mysql_version = sh(MYSQL + ["-N", "-e", "SELECT VERSION()"]).strip()
run_date = datetime.date.today().isoformat()

# ---------------------------------------------------------------------------
# 2. ER diagram (graphviz)
# ---------------------------------------------------------------------------
ER_NODES = {
    "city": ["city_id PK", "name", "state"],
    "theatre": ["theatre_id PK", "city_id FK", "name", "address"],
    "screen": ["screen_id PK", "theatre_id FK", "name"],
    "seat_category": ["seat_category_id PK", "name"],
    "seat": ["seat_id PK", "screen_id FK", "row_label", "seat_number", "seat_category_id FK"],
    "language": ["language_id PK", "name"],
    "show_format": ["format_id PK", "name"],
    "movie": ["movie_id PK", "title", "duration_minutes", "certificate", "release_date"],
    "movie_show": ["show_id PK", "screen_id FK", "movie_id FK", "language_id FK", "format_id FK",
                   "show_date", "start_time", "status"],
    "show_price": ["show_id PK,FK", "seat_category_id PK,FK", "price"],
    "app_user": ["user_id PK", "name", "email", "phone"],
    "booking": ["booking_id PK", "user_id FK", "show_id FK", "status", "hold_expires_at"],
    "show_seat": ["show_seat_id PK", "show_id FK", "seat_id FK", "status", "booking_id FK", "version"],
    "booking_seat": ["booking_id PK,FK", "show_seat_id PK,FK", "price_paid"],
    "payment": ["payment_id PK", "booking_id FK", "provider", "provider_txn_id", "amount", "status"],
    "payment_webhook_event": ["event_id PK", "provider", "provider_event_id", "provider_txn_id",
                              "event_type", "payload"],
}
ER_EDGES = [("theatre", "city"), ("screen", "theatre"), ("seat", "screen"), ("seat", "seat_category"),
            ("movie_show", "screen"), ("movie_show", "movie"), ("movie_show", "language"),
            ("movie_show", "show_format"), ("show_price", "movie_show"), ("show_price", "seat_category"),
            ("booking", "app_user"), ("booking", "movie_show"), ("show_seat", "movie_show"),
            ("show_seat", "seat"), ("show_seat", "booking"), ("booking_seat", "booking"),
            ("booking_seat", "show_seat"), ("payment", "booking")]
GROUP_FILL = {"city": "#E8EEF7", "theatre": "#E8EEF7", "screen": "#E8EEF7", "seat_category": "#E8EEF7",
              "seat": "#E8EEF7", "language": "#EEF3E6", "show_format": "#EEF3E6", "movie": "#EEF3E6",
              "movie_show": "#EEF3E6", "show_price": "#EEF3E6", "app_user": "#FBEFD9", "booking": "#FBEFD9",
              "show_seat": "#F9DCDC", "booking_seat": "#FBEFD9", "payment": "#FBEFD9",
              "payment_webhook_event": "#FBEFD9"}
dot = ['digraph ER {', 'rankdir=TB; nodesep=0.3; ranksep=0.55; splines=true;',
       'node [shape=plain, fontname="Helvetica", fontsize=10];', 'edge [color="#555555", arrowsize=0.8];']
for name, cols in ER_NODES.items():
    rows = "".join(f'<TR><TD ALIGN="LEFT" PORT="c{i}"><FONT POINT-SIZE="9">{c}</FONT></TD></TR>'
                   for i, c in enumerate(cols))
    dot.append(f'{name} [label=<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="3" '
               f'BGCOLOR="white"><TR><TD BGCOLOR="{GROUP_FILL[name]}"><B>{name}</B></TD></TR>{rows}</TABLE>>];')
for child, parent in ER_EDGES:
    dot.append(f'{child} -> {parent} [dir=both, arrowtail=crow, arrowhead=tee];')
dot.append('payment_webhook_event -> payment [style=dashed, arrowhead=none, '
           'label="logical (provider, txn id)", fontsize=8, fontname="Helvetica"];')
dot.append("}")
er_png = os.path.join(DOCS, "er_diagram.png")
subprocess.run(["dot", "-Tpng", "-Gdpi=170", "-o", er_png], input="\n".join(dot), text=True, check=True)

# ---------------------------------------------------------------------------
# 3. Document content (one structure, rendered to both Markdown and PDF)
# ---------------------------------------------------------------------------
C = []          # content blocks
def h1(t): C.append(("h1", t))
def h2(t): C.append(("h2", t))
def p(t): C.append(("p", t))
def bl(items): C.append(("bl", items))
def code(t): C.append(("code", t))
def tbl(header, rows, widths): C.append(("tbl", header, rows, widths))
def img(path): C.append(("img", path))
def pb(): C.append(("pb",))

C.append(("title", "BookMyShow-scale Ticketing Backend", "P1 (schema and locking strategy) and P2 (shows by theatre and date)"))
p(f"Target database: **MySQL 8.0+** (verified on {mysql_version}). Every query in this document was executed; "
  f"the example rows and outputs below are real output from a run on {run_date}. Show dates in the seed data are relative to "
  "`CURDATE()`, so the 'next 7 days' date picker always has data.")

h1("1. Scope and what is delivered")
bl(["**P1** - entities, attributes, table structures, `CREATE TABLE` SQL, sample rows, 1NF/2NF/3NF/BCNF analysis, and a "
    "locking strategy that prevents double-booked seats and lost holds (`sql/01_schema.sql`, `sql/02_seed.sql`, `sql/04_locking_patterns.sql`).",
    "**P2** - the query listing all shows at a theatre on a date with their timings (`sql/03_p2_queries.sql`).",
    "**Beyond the brief, to back the concurrency claim:** a runnable test that races 100+ real connections against the same "
    "rows and checks invariants inside the database (`tests/concurrency_test.py`). Results are in section 7.",
    "**Not built here:** the Redis hold layer, the payment-gateway integration, the HTTP API and the burst queue. "
    "Section 5 describes how Redis would sit in front of this schema; nothing in section 7 exercises Redis."])

pb()
h1("2. Entities and relationships")
p("Sixteen tables in four groups: venue catalogue (city, theatre, screen, seat_category, seat), film catalogue and scheduling "
  "(language, show_format, movie, movie_show, show_price), customers and orders (app_user, booking, booking_seat), and the two tables "
  "that carry the concurrency guarantees (show_seat, and the payment / payment_webhook_event pair). Crow's foot end = many side. "
  "The dashed line is a logical link only: webhook events are stored before we know they are valid, so it is deliberately not a foreign key.")
img(er_png)

pb()
h1("3. Tables, attributes and example rows")
p("Column types, keys and nullability come straight from `information_schema`. Auto-populated timestamp columns "
  "(created_at, updated_at, received_at) are omitted from the example rows to keep them readable.")
for t in TABLES:
    d = table_docs[t]
    C.append(("h2", t))
    p(PURPOSE[t])
    tbl(["Column", "Type", "Keys / flags", "Notes"], d["attrs"], [26, 26, 26, 22])
    p("Example rows:")
    if d["sample"]:
        n = len(d["sample_cols"])
        tbl(d["sample_cols"], d["sample"], [100 / n] * n)

h1("4. Normalization (1NF, 2NF, 3NF, BCNF)")
p("**1NF.** Every column holds one atomic value and there are no repeating groups. The seats of a booking are rows in "
  "`booking_seat`, not a list column; a movie's timings are rows in `movie_show`, not a comma list (the comma-joined view in P2 is "
  "produced by the query, not stored). The one deliberate exception is `payment_webhook_event.payload` (JSON): it is the gateway's raw "
  "document kept verbatim for audit and replay, and no query in this design reads inside it.")
p("**2NF.** Every table with a composite key has non-key columns that depend on the whole key. `show_price` (show_id, seat_category_id) -> price "
  "needs both halves: the same tier costs different amounts on different shows and different tiers cost different amounts on one show. "
  "`booking_seat` (booking_id, show_seat_id) -> price_paid likewise. All other tables use single-column surrogate keys.")
p("**3NF and BCNF.** In each table below, every determinant is a candidate key, so there are no transitive dependencies and BCNF holds.")
tbl(["Table", "Candidate keys", "Functional dependencies (all determinants are keys)"], [
    ["city", "city_id; (name, state)", "city_id -> name, state"],
    ["theatre", "theatre_id; (city_id, name)", "theatre_id -> city_id, name, address"],
    ["screen", "screen_id; (theatre_id, name)", "screen_id -> theatre_id, name"],
    ["seat_category / language / show_format", "id; name", "id <-> name"],
    ["seat", "seat_id; (screen_id, row_label, seat_number)", "seat_id -> screen_id, row, number, category"],
    ["movie", "movie_id; (title, release_date)", "movie_id -> title, duration, certificate, release_date"],
    ["movie_show", "show_id; (screen_id, show_date, start_time)", "show_id -> screen, movie, language, format, date, time, status"],
    ["show_price", "(show_id, seat_category_id)", "(show_id, category) -> price"],
    ["app_user", "user_id; email; phone", "user_id -> name, email, phone"],
    ["booking", "booking_id", "booking_id -> user, show, status, hold_expires_at, confirmed_at"],
    ["show_seat", "show_seat_id; (show_id, seat_id)", "show_seat_id -> show, seat, status, booking_id, version"],
    ["booking_seat", "(booking_id, show_seat_id)", "(booking, show_seat) -> price_paid"],
    ["payment", "payment_id; (provider, provider_txn_id)", "payment_id -> booking, provider, txn id, amount, status"],
    ["payment_webhook_event", "event_id; (provider, provider_event_id)", "event_id -> txn id, type, payload, times"],
], [20, 32, 48])
p("**Design decisions that keep this in 3NF** (each is a column we chose NOT to store):")
bl(["`movie_show` has no `theatre_id`: the theatre is determined by the screen (screen_id -> theatre_id), so storing it would be a transitive dependency. P2 reaches it through `screen`.",
    "`movie_show` has no `end_time`: it would be a function of (movie_id, start_time) - both non-key columns - which is a 3NF violation. P2 computes it as start_time + movie.duration_minutes.",
    "`booking` has no `total_amount`: it is SUM(booking_seat.price_paid), so it is computed, never stored, and can never disagree with its line items.",
    "`show_seat` has no `price`: it is derivable from show_price and seat.seat_category_id."])
p("**Two places where stored data looks redundant, and why each is a real fact rather than a duplicate:**")
bl(["`booking_seat.price_paid` is a snapshot of what the customer was charged. `show_price` can change after the sale, so the snapshot cannot be recomputed later; it is a historical fact.",
    "Ownership of a seat is recorded twice: `show_seat.booking_id` (current state, mutable) and `booking_seat` (immutable history). This is intentional. "
    "`show_seat.booking_id` is what allows a seat to be claimed with one single-row compare-and-set, which is the core of the locking strategy. "
    "It depends only on the key `show_seat_id`, so it passes 3NF/BCNF; the price of this design is that the release path must clear it, which the sweeper does (section 5).",
    "`seat.seat_category_id` is a per-seat attribute, not a per-row one, because real screens have mixed rows (recliner blocks, wheelchair bays). "
    "If a theatre guaranteed one tier per row, (screen_id, row_label) -> category would hold and this table would need to be split to stay in BCNF."])

h1("5. Locking strategy: no double bookings, no lost holds")
h2("5.1 Invariants and how the schema enforces each")
tbl(["Invariant", "Enforced by"], [
    ["I1. A seat in a show belongs to at most one booking", "UNIQUE (show_id, seat_id) so one row per seat per show; the hold is a conditional UPDATE (`... AND status='AVAILABLE'`), so InnoDB's row lock lets exactly one racer match; CHECK (status='AVAILABLE') iff booking_id IS NULL"],
    ["I2. A hold is all-or-nothing", "Insert booking + UPDATE seats + insert line items in one transaction; application compares ROW_COUNT() with the number of seats requested and rolls back on a shortfall"],
    ["I3. Holds expire and are never lost", "booking.hold_expires_at + a two-step, idempotent sweeper (section 5.4). A crash between the steps is finished by the next run"],
    ["I4. A payment event is applied at most once", "UNIQUE (provider, provider_event_id); insert-first in the same transaction as the business effect (section 5.5)"],
    ["I5. A late payment cannot revive an expired hold", "Confirm predicate `status='PENDING' AND hold_expires_at > NOW()`; 0 rows affected means refund"],
], [38, 62])
h2("5.2 Pessimistic vs optimistic locking")
tbl(["", "Pessimistic (SELECT ... FOR UPDATE)", "Optimistic (compare-and-set / version)"], [
    ["How", "Lock the rows, read, decide in the app, write, commit", "Write only if the row is still in the state we read (`status='AVAILABLE'`, or `version = ?`)"],
    ["Strength", "Simple reasoning when a decision spans many rows or needs a read first; no retries", "Nothing is locked while the app thinks; fails fast; one round trip"],
    ["Weakness", "Locks live across network round trips; hot seats form lock-wait queues; deadlocks unless lock order is fixed", "Under heavy contention many attempts fail and repeat work; app must handle 0-row updates"],
    ["Fits when", "Short critical section, frequent conflicts, multi-row rules", "Conflicts are rare or losing is an acceptable outcome"],
], [12, 44, 44])
p("**Choice.** The hot path (hold seats) is a single-statement compare-and-set inside a short transaction. InnoDB still takes a brief row lock while "
  "the UPDATE runs, so the database serialises racers; what we avoid is holding a lock across the application's thinking time. The usual downside "
  "of optimistic locking (retry storms) does not apply here, because a lost race on a seat is a business outcome, not an error to retry: "
  "the user is told the seat was just taken and picks another. The hold UPDATE runs as a range scan on `uq_show_seat` that examines only the requested rows "
  "(checked with EXPLAIN UPDATE), so it locks just those seats, in seat_id order. A consistent lock order removes the classic deadlock between "
  "two overlapping multi-seat requests, but it is not a proof that no deadlock can ever occur, so the application still retries on error 1213 (deadlock) and 1205 "
  "(lock timeout); section 7 reports how often that happened. The pessimistic form (`FOR UPDATE NOWAIT`, or `SKIP LOCKED` to auto-pick "
  "free seats) is included in `04_locking_patterns.sql` for flows that must read and apply rules across several rows before writing. "
  "The 10-minute payment window is never a database lock: a hold is a state (`HELD` plus an expiry), not an open transaction.")
h2("5.3 Where Redis fits (design only - not implemented or tested here)")
p("A Redis key per seat (`SET seat:{show}:{seat} <booking> NX PX 600000`, or a Lua script for all-or-nothing on several seats) would act as a "
  "fast gate in front of MySQL: requests for seats already held are rejected without touching the database, and the key's TTL gives an instant, "
  "timer-based release from the seat map. Correctness does not depend on it: if Redis is flushed or unavailable, the MySQL compare-and-set above still prevents "
  "a double booking. The sweeper reconciles any drift between Redis expiry and the database.")
h2("5.4 Hold expiry sweeper")
code("""-- D1: claim expired holds                      -- D2: release their seats
UPDATE booking SET status='EXPIRED'             UPDATE show_seat ss JOIN booking b ON b.booking_id=ss.booking_id
 WHERE status='PENDING'                            SET ss.status='AVAILABLE', ss.booking_id=NULL, ss.version=ss.version+1
   AND hold_expires_at <= NOW();                 WHERE ss.status='HELD' AND b.status IN ('EXPIRED','CANCELLED');""")
p("Both steps are idempotent and safe to run from many workers at once. D2 keys off the booking's status rather than off what D1 changed, so a crash between the "
  "two statements leaves nothing stuck: the next run releases those seats. That is what 'no lost holds' means in this design.")
h2("5.5 Idempotent payment webhooks")
p("The handler starts a transaction and inserts the event first. A redelivery hits the unique key (error 1062), so the handler rolls back, returns 200 and does nothing. "
  "The insert and the business effects (payment SUCCESS, booking CONFIRMED, seats BOOKED) commit together; if the process dies midway, the event row rolls back with "
  "the effects and the gateway's retry runs the whole thing again. If the hold has already expired, the confirm matches 0 rows and the handler flags the payment for refund "
  "instead of resurrecting the booking.")
h2("5.6 Indexes and the queries they serve")
tbl(["Index", "Serves"], [
    ["movie_show UNIQUE (screen_id, show_date, start_time)", "P2 lookup by screen and date; also stops a screen starting two shows at the same instant"],
    ["screen UNIQUE (theatre_id, name)", "P2: find a theatre's screens"],
    ["show_seat UNIQUE (show_id, seat_id)", "Row identity per show; the lock target and lock order for holds"],
    ["show_seat (show_id, status)", "Seat map and seats-left counts for a show"],
    ["show_seat (booking_id)", "Confirm / release all seats of a booking"],
    ["booking (status, hold_expires_at)", "Expiry sweeper scans only PENDING holds that are due"],
    ["payment_webhook_event UNIQUE (provider, provider_event_id)", "Idempotency key"],
    ["payment UNIQUE (provider, provider_txn_id)", "Match a webhook to its payment"],
], [46, 54])

h1("6. SQL")
h2("6.1 P1 - schema (sql/01_schema.sql)")
code(open(os.path.join(SQL, "01_schema.sql")).read().strip())
h2("6.2 P1 - sample data (sql/02_seed.sql)")
code(open(os.path.join(SQL, "02_seed.sql")).read().strip())
h2("6.3 P2 - shows at a theatre on a date (sql/03_p2_queries.sql)")
p("The first query is the direct answer: one row per show. The second returns one row per movie with its timings side by side, as in the reference screenshot. "
  "Bonus queries give the next-7-dates strip and seats left per show.")
code(open(os.path.join(SQL, "03_p2_queries.sql")).read().strip())
h2("6.4 P2 - output (theatre 1, today)")
code("\n\n".join(p2_blocks))
p("`ends_at` for the 10:00 PM Kalki show reads 01:01 AM because it is computed from the movie's 181-minute duration and correctly runs past midnight.")
h2("6.5 P2 - execution plan")
tbl(["table", "type", "key", "rows", "Extra"], explain_rows, [10, 12, 30, 8, 40])
p("The plan starts from `screen` on the (theatre_id, name) index and reaches `movie_show` through (screen_id, show_date, start_time) using the screen id and the date as an index prefix, "
  "so the number of rows examined depends on one theatre's shows for one day, not on the size of the show table. The temporary table and filesort apply only to that small result. "
  "The remaining joins are primary-key lookups.")
h2("6.6 Locking and idempotency statements (sql/04_locking_patterns.sql)")
code(open(os.path.join(SQL, "04_locking_patterns.sql")).read().strip())

h1("7. Concurrency test results")
p("`tests/concurrency_test.py` opens one real MySQL connection per simulated user, releases them all at the same instant with a barrier, and then checks invariants by querying the database, "
  "not by trusting what the threads report. Output of the run that produced this document:")
code(test_text)
p("**What this does and does not show.** In these runs, on one MySQL 8.0 server, the schema and statements above did not double-book a seat, left no partial group, applied a duplicated webhook once, "
  "released expired holds once, and refused a late payment. A passing run is evidence, not a proof; the guarantees themselves come from the constraints and conditional updates in section 5. It is a correctness test, not a benchmark: the elapsed times include opening 100 connections in a small sandbox and "
  "should not be read as throughput. It also runs against a single node with no Redis, no replicas and no gateway.")

h1("8. Assumptions and next steps")
bl(["Times are stored as MySQL DATETIME/TIME in server local time for the demo; production should store UTC and convert at the edge.",
    "`show_seat` is materialised per show (seats x shows rows). That is fine for one theatre chain; at national scale it should be partitioned by show_date and archived after the show.",
    "The application must only pass seat ids that belong to the show's screen; the hold UPDATE filters by show_id so a foreign seat simply matches no row.",
    "Next: the Redis gate (section 5.3), the burst queue / waiting room for on-sale spikes, and a load test sized for the real target."])

# ---------------------------------------------------------------------------
# 4. Renderers
# ---------------------------------------------------------------------------
def md_escape_cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def to_markdown(blocks):
    out = []
    for b in blocks:
        k = b[0]
        if k == "title":
            out += [f"# {b[1]}", f"*{b[2]}*", ""]
        elif k == "h1":
            out += [f"## {b[1]}", ""]
        elif k == "h2":
            out += [f"### {b[1]}", ""]
        elif k == "p":
            out += [b[1], ""]
        elif k == "bl":
            out += [f"- {i}" for i in b[1]] + [""]
        elif k == "code":
            out += ["```sql" if "SELECT" in b[1] or "CREATE" in b[1] else "```", b[1], "```", ""]
        elif k == "tbl":
            hdr, rows = b[1], b[2]
            out.append("| " + " | ".join(md_escape_cell(h) or " " for h in hdr) + " |")
            out.append("|" + "|".join(["---"] * len(hdr)) + "|")
            out += ["| " + " | ".join(md_escape_cell(c) for c in r) + " |" for r in rows]
            out.append("")
        elif k == "img":
            out += [f"![ER diagram]({os.path.basename(b[1])})", ""]
    return "\n".join(out)


with open(os.path.join(DOCS, "DESIGN.md"), "w") as fh:
    fh.write(to_markdown(C))

ss = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=ss["BodyText"], fontName="Helvetica", fontSize=9, leading=12.2, spaceAfter=5)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=7.4, leading=9.2, spaceAfter=0)
CELLB = ParagraphStyle("cellb", parent=CELL, fontName="Helvetica-Bold", textColor=colors.white)
H1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=14, spaceBefore=12, spaceAfter=6,
                    textColor=colors.HexColor("#1F3A5F"))
H2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=10.5, spaceBefore=8, spaceAfter=3,
                    textColor=colors.HexColor("#1F3A5F"))
TITLE = ParagraphStyle("title", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=22, alignment=TA_LEFT, spaceAfter=4,
                       textColor=colors.HexColor("#1F3A5F"))
SUB = ParagraphStyle("sub", parent=BODY, fontSize=11, textColor=colors.HexColor("#555555"), spaceAfter=10)
CODE = ParagraphStyle("code", fontName="Courier", fontSize=6.3, leading=7.6)
PAGE_W = A4[0] - 2 * 16 * mm


def inline(s):
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`(.+?)`", r'<font face="Courier" size="8">\1</font>', s)
    return s


def cell(s, style=CELL):
    txt = inline(str(s))
    # allow long identifiers to wrap
    txt = txt.replace("_", "_<wbr/>") if False else txt
    return Paragraph(txt, style)


def build_table(hdr, rows, widths):
    data = [[Paragraph(inline(h) or " ", CELLB) for h in hdr]] + [[cell(c) for c in r] for r in rows]
    tw = [PAGE_W * w / 100.0 for w in widths]
    t = Table(data, colWidths=tw, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A5F")),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#B8C2CF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6FA")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t


story = []
for b in C:
    k = b[0]
    if k == "title":
        story += [Paragraph(inline(b[1]), TITLE), Paragraph(inline(b[2]), SUB)]
    elif k == "h1":
        story.append(Paragraph(inline(b[1]), H1))
    elif k == "h2":
        story.append(Paragraph(inline(b[1]), H2))
    elif k == "p":
        story.append(Paragraph(inline(b[1]), BODY))
    elif k == "bl":
        for i in b[1]:
            story.append(Paragraph("&bull; " + inline(i), ParagraphStyle("li", parent=BODY, leftIndent=10, firstLineIndent=-8, spaceAfter=3)))
        story.append(Spacer(1, 3))
    elif k == "code":
        story.append(Preformatted(b[1], CODE, maxLineLength=None))
        story.append(Spacer(1, 5))
    elif k == "tbl":
        story += [build_table(b[1], b[2], b[3]), Spacer(1, 5)]
    elif k == "img":
        from PIL import Image as PILImage
        w, h = PILImage.open(b[1]).size
        scale = min(PAGE_W / w, 205 * mm / h)
        story.append(Image(b[1], width=w * scale, height=h * scale))
    elif k == "pb":
        story.append(PageBreak())


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.HexColor("#777777"))
    canvas.drawString(16 * mm, 9 * mm, "BookMyShow-scale Ticketing Backend - P1 and P2")
    canvas.drawRightString(A4[0] - 16 * mm, 9 * mm, f"Page {doc.page}")
    canvas.restoreState()


pdf_path = os.path.join(DOCS, "BookMyShow_P1_P2.pdf")
SimpleDocTemplate(pdf_path, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                  bottomMargin=16 * mm, title="BookMyShow-scale Ticketing Backend - P1 and P2",
                  author="Anjeee").build(story, onFirstPage=footer, onLaterPages=footer)
print("built:", pdf_path)
