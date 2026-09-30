-- =====================================================================
-- Sample data.  Show dates are relative to CURDATE() so the "next 7 days"
-- date picker always has data whenever this script is run.
-- =====================================================================
USE bookmyshow;

INSERT INTO city (name, state) VALUES ('Pune', 'Maharashtra'), ('Mumbai', 'Maharashtra');

INSERT INTO theatre (city_id, name, address) VALUES
  (1, 'PVR Phoenix Marketcity', 'Viman Nagar, Pune'),
  (1, 'INOX Amanora',           'Hadapsar, Pune'),
  (2, 'PVR Juhu',               'Juhu, Mumbai');

INSERT INTO screen (theatre_id, name) VALUES
  (1, 'Audi 1'), (1, 'Audi 2'),      -- screen_id 1, 2  (PVR Phoenix)
  (2, 'Audi 1'),                     -- screen_id 3     (INOX Amanora)
  (3, 'Audi 1');                     -- screen_id 4     (PVR Juhu)

INSERT INTO seat_category (name) VALUES ('SILVER'), ('GOLD'), ('RECLINER');

-- Seat layout: 3 rows x 8 seats per screen (A = SILVER, B = GOLD, C = RECLINER)
INSERT INTO seat (screen_id, row_label, seat_number, seat_category_id)
SELECT sc.screen_id, r.row_label, n.n, r.cat
FROM screen sc
CROSS JOIN (SELECT 'A' AS row_label, 1 AS cat UNION ALL
            SELECT 'B', 2 UNION ALL
            SELECT 'C', 3) r
CROSS JOIN (SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL
            SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8) n;

INSERT INTO language (name) VALUES ('Hindi'), ('English'), ('Telugu');
INSERT INTO show_format (name) VALUES ('2D'), ('3D'), ('IMAX 2D');

INSERT INTO movie (title, duration_minutes, certificate, release_date) VALUES
  ('Kalki 2898 AD',       181, 'UA', '2024-06-27'),
  ('Dune: Part Two',      166, 'UA', '2024-03-01'),
  ('Inside Out 2',         96, 'U',  '2024-06-14');

-- ---------------------------------------------------------------------
-- Shows at theatre 1 (PVR Phoenix) for the next 7 days, plus a few elsewhere
-- ---------------------------------------------------------------------
-- Audi 1: Kalki (Hindi, 2D) at 10:00, 14:00, 18:30 and 22:00 every day
INSERT INTO movie_show (screen_id, movie_id, language_id, format_id, show_date, start_time)
SELECT 1, 1, 1, 1, CURDATE() + INTERVAL d.n DAY, t.st
FROM (SELECT 0 AS n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL
      SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6) d
CROSS JOIN (SELECT '10:00:00' AS st UNION ALL SELECT '14:00:00' UNION ALL
            SELECT '18:30:00' UNION ALL SELECT '22:00:00') t;

-- Audi 2: Dune Part Two (English, IMAX 2D) at 11:30, 15:30, 20:00 for next 7 days
INSERT INTO movie_show (screen_id, movie_id, language_id, format_id, show_date, start_time)
SELECT 2, 2, 2, 3, CURDATE() + INTERVAL d.n DAY, t.st
FROM (SELECT 0 AS n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL
      SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6) d
CROSS JOIN (SELECT '11:30:00' AS st UNION ALL SELECT '15:30:00' UNION ALL
            SELECT '20:00:00') t;

-- Audi 2 also runs Inside Out 2 (English, 2D) at 09:00 today only
INSERT INTO movie_show (screen_id, movie_id, language_id, format_id, show_date, start_time)
VALUES (2, 3, 2, 1, CURDATE(), '09:00:00');

-- Other theatres (to prove the theatre filter works)
INSERT INTO movie_show (screen_id, movie_id, language_id, format_id, show_date, start_time) VALUES
  (3, 1, 3, 2, CURDATE(), '13:00:00'),    -- INOX Amanora: Kalki (Telugu, 3D)
  (4, 2, 2, 1, CURDATE(), '19:00:00');    -- PVR Juhu: Dune 2

-- Per-show, per-tier pricing
INSERT INTO show_price (show_id, seat_category_id, price)
SELECT s.show_id, c.seat_category_id,
       CASE c.name WHEN 'SILVER' THEN 250.00 WHEN 'GOLD' THEN 350.00 ELSE 550.00 END
FROM movie_show s CROSS JOIN seat_category c;

-- Materialise the seat inventory for every show (all AVAILABLE)
INSERT INTO show_seat (show_id, seat_id)
SELECT s.show_id, st.seat_id
FROM movie_show s JOIN seat st ON st.screen_id = s.screen_id;

-- ---------------------------------------------------------------------
-- Users + a few bookings in different states
-- ---------------------------------------------------------------------
INSERT INTO app_user (name, email, phone) VALUES
  ('Aarav Sharma', 'aarav@example.com', '+919800000001'),
  ('Diya Patel',   'diya@example.com',  '+919800000002'),
  ('Kabir Singh',  'kabir@example.com', '+919800000003');

-- show_id 1 = Audi 1, today 22:00 (Kalki).  Seats B1,B2 (Gold) confirmed for Aarav.
INSERT INTO booking (user_id, show_id, status, hold_expires_at, confirmed_at)
VALUES (1, 1, 'CONFIRMED', NOW() + INTERVAL 10 MINUTE, NOW());

-- Diya is mid-checkout for C4,C5 (Recliner): PENDING with a live 10-min hold.
INSERT INTO booking (user_id, show_id, status, hold_expires_at)
VALUES (2, 1, 'PENDING', NOW() + INTERVAL 10 MINUTE);

-- Kabir's hold lapsed: EXPIRED, seats already released.
INSERT INTO booking (user_id, show_id, status, hold_expires_at)
VALUES (3, 1, 'EXPIRED', NOW() - INTERVAL 5 MINUTE);

-- Attach seats (show_seat rows for show 1: A1..A8 = ids 1..8, B1..B8 = 9..16, C1..C8 = 17..24)
UPDATE show_seat SET status = 'BOOKED', booking_id = 1, version = version + 1
WHERE show_id = 1 AND seat_id IN (SELECT seat_id FROM seat WHERE screen_id = 1 AND row_label = 'B' AND seat_number IN (1,2));

UPDATE show_seat SET status = 'HELD', booking_id = 2, version = version + 1
WHERE show_id = 1 AND seat_id IN (SELECT seat_id FROM seat WHERE screen_id = 1 AND row_label = 'C' AND seat_number IN (4,5));

INSERT INTO booking_seat (booking_id, show_seat_id, price_paid)
SELECT ss.booking_id, ss.show_seat_id, sp.price
FROM show_seat ss
JOIN seat st       ON st.seat_id = ss.seat_id
JOIN show_price sp ON sp.show_id = ss.show_id AND sp.seat_category_id = st.seat_category_id
WHERE ss.booking_id IN (1, 2);

-- Kabir's expired line item (history kept even though the seat was released): A1 of show 1
INSERT INTO booking_seat (booking_id, show_seat_id, price_paid) VALUES (3, 1, 250.00);

-- Payments + webhook inbox
INSERT INTO payment (booking_id, provider, provider_txn_id, amount, status) VALUES
  (1, 'razorpay', 'pay_Nx001', 700.00,  'SUCCESS'),
  (2, 'razorpay', 'pay_Nx002', 1100.00, 'INITIATED');

INSERT INTO payment_webhook_event (provider, provider_event_id, provider_txn_id, event_type, payload, processed_at)
VALUES ('razorpay', 'evt_0001', 'pay_Nx001', 'payment.captured',
        JSON_OBJECT('amount', 70000, 'currency', 'INR', 'status', 'captured'), NOW());
