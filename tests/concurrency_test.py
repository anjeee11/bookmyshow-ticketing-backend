#!/usr/bin/env python3
"""
Concurrency proof for the BookMyShow schema (MySQL 8).

Each scenario fires many real connections at the same rows at the same instant and
then checks invariants in the database itself (not just what the threads report).

    pip install pymysql
    python tests/concurrency_test.py            # uses root@localhost via unix socket
    DB_HOST=127.0.0.1 DB_USER=root DB_PASSWORD=... python tests/concurrency_test.py

Requires 01_schema.sql and 02_seed.sql to have been loaded.
"""
import os
import random
import sys
import threading
import time

import pymysql
from pymysql.err import IntegrityError, OperationalError

CFG = dict(
    host=os.getenv("DB_HOST", "localhost"),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database="bookmyshow",
    autocommit=False,
    unix_socket=None if os.getenv("DB_HOST") else "/var/run/mysqld/mysqld.sock",
)
DEADLOCK, LOCK_TIMEOUT = 1213, 1205
RETRIES = {"n": 0}
_retry_lock = threading.Lock()


def connect():
    cfg = {k: v for k, v in CFG.items() if v is not None}
    return pymysql.connect(**cfg)


# --------------------------------------------------------------------------
# The application-side operations (same SQL as sql/04_locking_patterns.sql)
# --------------------------------------------------------------------------
def hold_seats(conn, user_id, show_id, seat_ids, ttl_seconds=600):
    """All-or-nothing seat hold via compare-and-set. Returns booking_id or None."""
    for _attempt in range(5):                       # retry only on deadlock / lock timeout
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO booking (user_id, show_id, status, hold_expires_at) "
                    "VALUES (%s, %s, 'PENDING', NOW() + INTERVAL %s SECOND)",
                    (user_id, show_id, ttl_seconds))
                booking_id = cur.lastrowid
                marks = ",".join(["%s"] * len(seat_ids))
                cur.execute(
                    f"UPDATE show_seat SET status='HELD', booking_id=%s, version=version+1 "
                    f"WHERE show_id=%s AND seat_id IN ({marks}) AND status='AVAILABLE'",
                    (booking_id, show_id, *seat_ids))
                if cur.rowcount != len(seat_ids):    # someone else owns at least one seat
                    conn.rollback()
                    return None
                cur.execute(
                    "INSERT INTO booking_seat (booking_id, show_seat_id, price_paid) "
                    "SELECT ss.booking_id, ss.show_seat_id, sp.price FROM show_seat ss "
                    "JOIN seat st ON st.seat_id=ss.seat_id "
                    "JOIN show_price sp ON sp.show_id=ss.show_id AND sp.seat_category_id=st.seat_category_id "
                    "WHERE ss.booking_id=%s", (booking_id,))
            conn.commit()
            return booking_id
        except OperationalError as e:
            conn.rollback()
            if e.args[0] not in (DEADLOCK, LOCK_TIMEOUT):
                raise
            with _retry_lock:
                RETRIES["n"] += 1
            time.sleep(random.random() * 0.01)
    return None


def process_webhook(conn, provider, event_id, txn_id, event_type):
    """Insert-first idempotent webhook. Returns PROCESSED / DUPLICATE / REFUND_REQUIRED."""
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO payment_webhook_event (provider, provider_event_id, provider_txn_id, event_type, payload) "
                    "VALUES (%s,%s,%s,%s,JSON_OBJECT('sim', 1))", (provider, event_id, txn_id, event_type))
            except IntegrityError as e:
                if e.args[0] == 1062:                # already seen this event -> no-op
                    conn.rollback()
                    return "DUPLICATE"
                raise
            cur.execute("SELECT booking_id FROM payment WHERE provider=%s AND provider_txn_id=%s FOR UPDATE",
                        (provider, txn_id))
            booking_id = cur.fetchone()[0]
            cur.execute("UPDATE payment SET status='SUCCESS' WHERE provider=%s AND provider_txn_id=%s "
                        "AND status='INITIATED'", (provider, txn_id))
            cur.execute("UPDATE booking SET status='CONFIRMED', confirmed_at=NOW() "
                        "WHERE booking_id=%s AND status='PENDING' AND hold_expires_at > NOW()", (booking_id,))
            confirmed = cur.rowcount == 1
            if confirmed:
                cur.execute("UPDATE show_seat SET status='BOOKED', version=version+1 "
                            "WHERE booking_id=%s AND status='HELD'", (booking_id,))
            cur.execute("UPDATE payment_webhook_event SET processed_at=NOW() "
                        "WHERE provider=%s AND provider_event_id=%s", (provider, event_id))
        conn.commit()
        return "PROCESSED" if confirmed else "REFUND_REQUIRED"
    except OperationalError as e:
        conn.rollback()
        if e.args[0] in (DEADLOCK, LOCK_TIMEOUT):
            return process_webhook(conn, provider, event_id, txn_id, event_type)
        raise


def sweep_expired(conn):
    with conn.cursor() as cur:
        cur.execute("UPDATE booking SET status='EXPIRED' WHERE status='PENDING' AND hold_expires_at <= NOW()")
        cur.execute("UPDATE show_seat ss JOIN booking b ON b.booking_id=ss.booking_id "
                    "SET ss.status='AVAILABLE', ss.booking_id=NULL, ss.version=ss.version+1 "
                    "WHERE ss.status='HELD' AND b.status IN ('EXPIRED','CANCELLED')")
    conn.commit()


# --------------------------------------------------------------------------
# Harness helpers
# --------------------------------------------------------------------------
def run_parallel(n, fn):
    """Start n threads, release them at the same instant, collect results."""
    barrier, results, errors = threading.Barrier(n), [None] * n, []

    def worker(i):
        conn = connect()
        try:
            barrier.wait()
            results[i] = fn(conn, i)
        except Exception as e:                       # surfaced below, fails the test
            errors.append(repr(e))
        finally:
            conn.close()

    t0 = time.time()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    if errors:
        raise RuntimeError(f"{len(errors)} worker error(s), e.g. {errors[0]}")
    return results, time.time() - t0


def reset_show(show_id):
    """Put a show back to a clean, all-AVAILABLE state and drop test bookings on it."""
    c = connect()
    with c.cursor() as cur:
        cur.execute("UPDATE show_seat SET status='AVAILABLE', booking_id=NULL, version=0 WHERE show_id=%s", (show_id,))
        cur.execute("DELETE FROM payment_webhook_event WHERE provider_txn_id IN "
                    "(SELECT provider_txn_id FROM payment WHERE booking_id IN (SELECT booking_id FROM booking WHERE show_id=%s))",
                    (show_id,))
        cur.execute("DELETE FROM payment WHERE booking_id IN (SELECT booking_id FROM booking WHERE show_id=%s)", (show_id,))
        cur.execute("DELETE FROM booking_seat WHERE booking_id IN (SELECT booking_id FROM booking WHERE show_id=%s)", (show_id,))
        cur.execute("DELETE FROM booking WHERE show_id=%s", (show_id,))
    c.commit()
    c.close()


def q(sql, args=()):
    c = connect()
    with c.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    c.commit()
    c.close()
    return rows


def seat_ids_for(show_id, labels):
    marks = ",".join(["%s"] * len(labels))
    rows = q(f"SELECT seat_id FROM seat st JOIN movie_show s ON s.screen_id=st.screen_id "
             f"WHERE s.show_id=%s AND CONCAT(st.row_label, st.seat_number) IN ({marks}) ORDER BY st.seat_id",
             (show_id, *labels))
    return [r[0] for r in rows]


FAILS = []


def check(name, cond, detail=""):
    print(f"   [{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        FAILS.append(name)


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------
def scenario_1_same_seat(n=100):
    """N users click the SAME seat at the same instant. Exactly one may get it."""
    print(f"\n1) {n} concurrent users, ONE seat (show 34, seat A1)")
    show = 34
    reset_show(show)
    seat = seat_ids_for(show, ["A1"])
    results, secs = run_parallel(n, lambda c, i: hold_seats(c, 1 + i % 3, show, seat))
    winners = [r for r in results if r]
    check("exactly one winner", len(winners) == 1, f"(winners={len(winners)}, {secs:.2f}s)")
    st = q("SELECT status, COUNT(*), MAX(version) FROM show_seat WHERE show_id=%s AND seat_id=%s GROUP BY status",
           (show, seat[0]))
    check("seat is HELD by the winner, version bumped once", st == (("HELD", 1, 1),), str(st))
    orphan = q("SELECT COUNT(*) FROM booking b WHERE show_id=%s AND status='PENDING' AND "
               "NOT EXISTS (SELECT 1 FROM booking_seat bs WHERE bs.booking_id=b.booking_id)", (show,))[0][0]
    check("losers left no ghost bookings behind", orphan == 0, f"(orphans={orphan})")


def scenario_2_overlapping_sets(n=120):
    """Users grab random overlapping 3-seat groups from 12 seats: no partial holds, no double-owned seat."""
    print(f"\n2) {n} concurrent users, overlapping 3-seat groups from 12 seats (show 33)")
    show = 33
    reset_show(show)
    pool = seat_ids_for(show, [f"{r}{n_}" for r in "AB" for n_ in range(1, 7)])
    random.seed(7)
    picks = [sorted(random.sample(pool, 3)) for _ in range(n)]
    results, secs = run_parallel(n, lambda c, i: hold_seats(c, 1 + i % 3, show, picks[i]))
    winners = [r for r in results if r]
    held = q("SELECT COUNT(*) FROM show_seat WHERE show_id=%s AND status='HELD'", (show,))[0][0]
    check("held seats == 3 x successful bookings (all-or-nothing)", held == 3 * len(winners),
          f"(held={held}, winners={len(winners)}, {secs:.2f}s)")
    bad = q("SELECT COUNT(*) FROM (SELECT booking_id FROM show_seat WHERE show_id=%s AND booking_id IS NOT NULL "
            "GROUP BY booking_id HAVING COUNT(*) <> 3) x", (show,))[0][0]
    check("no booking owns a partial group", bad == 0)
    dup = q("SELECT COUNT(*) FROM (SELECT show_id, seat_id FROM show_seat WHERE show_id=%s "
            "GROUP BY show_id, seat_id HAVING COUNT(*) > 1) x", (show,))[0][0]
    check("no seat row duplicated for the show", dup == 0)
    ledger = q("SELECT COUNT(*) FROM booking_seat bs JOIN booking b USING (booking_id) WHERE b.show_id=%s AND b.status='PENDING'",
               (show,))[0][0]
    check("line items match owned seats", ledger == held, f"(ledger={ledger})")
    print(f"   [INFO] deadlock/lock-timeout retries across scenarios 1-2: {RETRIES['n']}")


def scenario_3_webhook_idempotency(n=100):
    """The gateway delivers the same webhook N times in parallel. Business effect happens once."""
    print(f"\n3) {n} duplicate deliveries of ONE payment webhook (show 32)")
    show = 32
    reset_show(show)
    seat = seat_ids_for(show, ["B1", "B2"])
    conn = connect()
    booking_id = hold_seats(conn, 1, show, seat)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO payment (booking_id, provider, provider_txn_id, amount) VALUES (%s,'razorpay','pay_race_1',700)",
                    (booking_id,))
    conn.commit()
    conn.close()
    results, secs = run_parallel(n, lambda c, i: process_webhook(c, "razorpay", "evt_race_1", "pay_race_1", "payment.captured"))
    proc = results.count("PROCESSED")
    check("processed exactly once", proc == 1 and results.count("DUPLICATE") == n - 1,
          f"(processed={proc}, duplicates={results.count('DUPLICATE')}, {secs:.2f}s)")
    events = q("SELECT COUNT(*) FROM payment_webhook_event WHERE provider_event_id='evt_race_1'")[0][0]
    check("one event row stored", events == 1)
    st = q("SELECT status FROM booking WHERE booking_id=%s", (booking_id,))[0][0]
    seats = q("SELECT status, MAX(version) FROM show_seat WHERE booking_id=%s GROUP BY status", (booking_id,))
    check("booking CONFIRMED, seats BOOKED with a single version bump each",
          st == "CONFIRMED" and seats == (("BOOKED", 2),), f"({st}, {seats})")


def scenario_4_expiry(n=50):
    """Holds expire on time, concurrent sweepers release exactly once, late payment is not honoured."""
    print(f"\n4) hold expiry: {n} concurrent sweepers + a late payment (show 32)")
    show = 32
    reset_show(show)
    seat = seat_ids_for(show, ["C1", "C2"])
    conn = connect()
    booking_id = hold_seats(conn, 2, show, seat, ttl_seconds=1)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO payment (booking_id, provider, provider_txn_id, amount) VALUES (%s,'razorpay','pay_late_1',1100)",
                    (booking_id,))
    conn.commit()
    conn.close()
    time.sleep(2.2)                                   # let the 1-second hold lapse
    _, secs = run_parallel(n, lambda c, i: sweep_expired(c))
    rows = q("SELECT status, booking_id, version FROM show_seat WHERE show_id=%s AND seat_id IN (%s,%s)", (show, *seat))
    check("seats released back to AVAILABLE exactly once", all(r == ("AVAILABLE", None, 2) for r in rows),
          f"({rows}, {secs:.2f}s)")
    check("booking marked EXPIRED", q("SELECT status FROM booking WHERE booking_id=%s", (booking_id,))[0][0] == "EXPIRED")
    late = process_webhook(connect(), "razorpay", "evt_late_1", "pay_late_1", "payment.captured")
    after = q("SELECT status FROM booking WHERE booking_id=%s", (booking_id,))[0][0]
    check("late payment does NOT resurrect the booking (flagged for refund)",
          late == "REFUND_REQUIRED" and after == "EXPIRED", f"({late}, {after})")
    # and the freed seats can be sold again immediately
    again = hold_seats(connect(), 3, show, seat)
    check("released seats can be re-held", again is not None)


if __name__ == "__main__":
    root = connect()
    with root.cursor() as cur:
        cur.execute("SET GLOBAL max_connections = 600")   # default 151 is below our thread count
    root.close()
    try:
        scenario_1_same_seat()
        scenario_2_overlapping_sets()
        scenario_3_webhook_idempotency()
        scenario_4_expiry()
    finally:
        for s in (32, 33, 34):
            reset_show(s)
    print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)
