-- =====================================================================
-- Locking strategy: the statements the application runs.
-- Every transition is a compare-and-set on show_seat / booking, so the
-- database (not application code) is the arbiter of who wins a seat.
-- This file is runnable end to end; it books seats for show 32 (Dune, today 8 PM).
-- =====================================================================
USE bookmyshow;

SET @user_id = 2;
SET @show_id = 32;
-- seats A1, A2  ->  resolved to show_seat rows
SET @seat_a1 = (SELECT seat_id FROM seat WHERE screen_id = 2 AND row_label = 'A' AND seat_number = 1);
SET @seat_a2 = (SELECT seat_id FROM seat WHERE screen_id = 2 AND row_label = 'A' AND seat_number = 2);

-- ---------------------------------------------------------------------
-- A. HOLD SEATS  (default path: optimistic compare-and-set, all-or-nothing)
--    The `status = 'AVAILABLE'` predicate is the compare; InnoDB's row lock
--    serialises concurrent UPDATEs, so of N racers exactly one matches.
--    Rows are locked in unique-index order (show_id, seat_id) which gives a
--    consistent lock order across sessions -> no deadlocks between them.
-- ---------------------------------------------------------------------
START TRANSACTION;

INSERT INTO booking (user_id, show_id, status, hold_expires_at)
VALUES (@user_id, @show_id, 'PENDING', NOW() + INTERVAL 10 MINUTE);
SET @booking_id = LAST_INSERT_ID();

UPDATE show_seat
   SET status = 'HELD', booking_id = @booking_id, version = version + 1
 WHERE show_id = @show_id
   AND seat_id IN (@seat_a1, @seat_a2)
   AND status  = 'AVAILABLE';

SET @claimed = ROW_COUNT();       -- application: if @claimed <> 2 -> ROLLBACK and tell the user
SELECT @claimed AS seats_claimed; -- (requested = 2)

INSERT INTO booking_seat (booking_id, show_seat_id, price_paid)
SELECT ss.booking_id, ss.show_seat_id, sp.price
FROM show_seat ss
JOIN seat st       ON st.seat_id = ss.seat_id
JOIN show_price sp ON sp.show_id = ss.show_id AND sp.seat_category_id = st.seat_category_id
WHERE ss.booking_id = @booking_id;

COMMIT;

-- ---------------------------------------------------------------------
-- B. HOLD SEATS  (pessimistic alternative: lock first, decide, then write)
--    NOWAIT fails immediately (error 3572) instead of queueing behind a
--    competing session; SKIP LOCKED would silently skip locked rows.
--    ORDER BY seat_id keeps the lock order identical for every session.
--    Shown for comparison; run inside a transaction, then COMMIT/ROLLBACK.
-- ---------------------------------------------------------------------
-- START TRANSACTION;
-- SELECT show_seat_id, status
--   FROM show_seat
--  WHERE show_id = @show_id AND seat_id IN (@seat_a1, @seat_a2)
--  ORDER BY seat_id
--    FOR UPDATE NOWAIT;
-- -- application checks every row is AVAILABLE, then:
-- UPDATE show_seat SET status='HELD', booking_id=@booking_id, version=version+1
--  WHERE show_id=@show_id AND seat_id IN (@seat_a1,@seat_a2);
-- COMMIT;

-- ---------------------------------------------------------------------
-- C. CONFIRM after successful payment  (only if the hold is still alive)
-- ---------------------------------------------------------------------
START TRANSACTION;

UPDATE booking
   SET status = 'CONFIRMED', confirmed_at = NOW()
 WHERE booking_id = @booking_id
   AND status = 'PENDING'
   AND hold_expires_at > NOW();
SET @confirmed = ROW_COUNT();     -- 0 => hold expired/cancelled: payment must be refunded

UPDATE show_seat
   SET status = 'BOOKED', version = version + 1
 WHERE booking_id = @booking_id AND status = 'HELD' AND @confirmed = 1;

COMMIT;
SELECT @confirmed AS booking_confirmed;

-- ---------------------------------------------------------------------
-- D. EXPIRY SWEEPER  (run every few seconds; safe to run concurrently)
--    Two idempotent steps. If the process dies between them, the next run
--    finishes the job, so a hold can never be "lost" (stuck HELD forever).
-- ---------------------------------------------------------------------
-- D1. claim expired holds
UPDATE booking
   SET status = 'EXPIRED'
 WHERE status = 'PENDING' AND hold_expires_at <= NOW();

-- D2. release the seats of every EXPIRED/CANCELLED booking that are still HELD
UPDATE show_seat ss
  JOIN booking b ON b.booking_id = ss.booking_id
   SET ss.status = 'AVAILABLE', ss.booking_id = NULL, ss.version = ss.version + 1
 WHERE ss.status = 'HELD' AND b.status IN ('EXPIRED', 'CANCELLED');

-- ---------------------------------------------------------------------
-- E. IDEMPOTENT PAYMENT WEBHOOK  (insert-first)
--    The UNIQUE (provider, provider_event_id) key makes redelivery harmless:
--    the duplicate INSERT fails with error 1062, the handler returns 200 and
--    does nothing else. Insert + business effects share one transaction, so
--    a crash mid-processing rolls the event row back and the retry re-runs it.
-- ---------------------------------------------------------------------
START TRANSACTION;

INSERT INTO payment_webhook_event (provider, provider_event_id, provider_txn_id, event_type, payload)
VALUES ('razorpay', 'evt_demo_0002', 'pay_Nx002', 'payment.captured',
        JSON_OBJECT('amount', 110000, 'currency', 'INR'));
-- error 1062 here => duplicate delivery => ROLLBACK, respond 200 OK, stop.

UPDATE payment SET status = 'SUCCESS'
 WHERE provider = 'razorpay' AND provider_txn_id = 'pay_Nx002' AND status = 'INITIATED';

UPDATE payment_webhook_event SET processed_at = NOW()
 WHERE provider = 'razorpay' AND provider_event_id = 'evt_demo_0002';

COMMIT;
