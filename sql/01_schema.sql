-- =====================================================================
-- BookMyShow-scale ticketing backend  |  P1: schema  |  MySQL 8.0+
-- Run:  mysql -u root -p < sql/01_schema.sql
-- =====================================================================
DROP DATABASE IF EXISTS bookmyshow;
CREATE DATABASE bookmyshow CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE bookmyshow;

-- ---------------------------------------------------------------------
-- Reference / catalogue tables
-- ---------------------------------------------------------------------
CREATE TABLE city (
  city_id  INT UNSIGNED NOT NULL AUTO_INCREMENT,
  name     VARCHAR(80)  NOT NULL,
  state    VARCHAR(80)  NOT NULL,
  PRIMARY KEY (city_id),
  UNIQUE KEY uq_city_name_state (name, state)
) ENGINE=InnoDB;

CREATE TABLE theatre (
  theatre_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  city_id    INT UNSIGNED NOT NULL,
  name       VARCHAR(120) NOT NULL,
  address    VARCHAR(255) NOT NULL,
  PRIMARY KEY (theatre_id),
  UNIQUE KEY uq_theatre_city_name (city_id, name),
  CONSTRAINT fk_theatre_city FOREIGN KEY (city_id) REFERENCES city (city_id)
) ENGINE=InnoDB;

CREATE TABLE screen (            -- an auditorium inside a theatre
  screen_id  INT UNSIGNED NOT NULL AUTO_INCREMENT,
  theatre_id INT UNSIGNED NOT NULL,
  name       VARCHAR(40)  NOT NULL,          -- e.g. 'Audi 1'
  PRIMARY KEY (screen_id),
  UNIQUE KEY uq_screen_theatre_name (theatre_id, name),
  CONSTRAINT fk_screen_theatre FOREIGN KEY (theatre_id) REFERENCES theatre (theatre_id)
) ENGINE=InnoDB;

CREATE TABLE seat_category (     -- pricing tier: SILVER / GOLD / RECLINER
  seat_category_id TINYINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name             VARCHAR(30) NOT NULL,
  PRIMARY KEY (seat_category_id),
  UNIQUE KEY uq_seat_category_name (name)
) ENGINE=InnoDB;

CREATE TABLE seat (              -- physical seat; exists once per screen, independent of shows
  seat_id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  screen_id        INT UNSIGNED NOT NULL,
  row_label        VARCHAR(2)   NOT NULL,     -- 'A', 'B', ...
  seat_number      SMALLINT UNSIGNED NOT NULL,
  seat_category_id TINYINT UNSIGNED NOT NULL,
  PRIMARY KEY (seat_id),
  UNIQUE KEY uq_seat_position (screen_id, row_label, seat_number),
  CONSTRAINT fk_seat_screen   FOREIGN KEY (screen_id)        REFERENCES screen (screen_id),
  CONSTRAINT fk_seat_category FOREIGN KEY (seat_category_id) REFERENCES seat_category (seat_category_id)
) ENGINE=InnoDB;

CREATE TABLE language (
  language_id SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name        VARCHAR(40) NOT NULL,
  PRIMARY KEY (language_id),
  UNIQUE KEY uq_language_name (name)
) ENGINE=InnoDB;

CREATE TABLE show_format (       -- 2D / 3D / IMAX 2D ...
  format_id SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name      VARCHAR(30) NOT NULL,
  PRIMARY KEY (format_id),
  UNIQUE KEY uq_show_format_name (name)
) ENGINE=InnoDB;

CREATE TABLE movie (
  movie_id         INT UNSIGNED NOT NULL AUTO_INCREMENT,
  title            VARCHAR(200) NOT NULL,
  duration_minutes SMALLINT UNSIGNED NOT NULL,
  certificate      VARCHAR(5)   NOT NULL,     -- U, UA, A ...
  release_date     DATE         NOT NULL,
  PRIMARY KEY (movie_id),
  UNIQUE KEY uq_movie_title_release (title, release_date),
  CONSTRAINT chk_movie_duration CHECK (duration_minutes > 0)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- Scheduling.  (`show` is a reserved word in MySQL, hence movie_show.)
-- end_time is deliberately NOT stored: it is start_time + movie.duration
-- (a transitive dependency), so P2 computes it. See docs for the reasoning.
-- ---------------------------------------------------------------------
CREATE TABLE movie_show (
  show_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  screen_id   INT UNSIGNED NOT NULL,
  movie_id    INT UNSIGNED NOT NULL,
  language_id SMALLINT UNSIGNED NOT NULL,
  format_id   SMALLINT UNSIGNED NOT NULL,
  show_date   DATE NOT NULL,
  start_time  TIME NOT NULL,
  status      ENUM('SCHEDULED','CANCELLED','COMPLETED') NOT NULL DEFAULT 'SCHEDULED',
  PRIMARY KEY (show_id),
  -- one screen cannot start two shows at the same moment; also the access path for P2
  UNIQUE KEY uq_show_screen_slot (screen_id, show_date, start_time),
  KEY idx_show_movie (movie_id, show_date),
  CONSTRAINT fk_show_screen   FOREIGN KEY (screen_id)   REFERENCES screen (screen_id),
  CONSTRAINT fk_show_movie    FOREIGN KEY (movie_id)    REFERENCES movie (movie_id),
  CONSTRAINT fk_show_language FOREIGN KEY (language_id) REFERENCES language (language_id),
  CONSTRAINT fk_show_format   FOREIGN KEY (format_id)   REFERENCES show_format (format_id)
) ENGINE=InnoDB;

CREATE TABLE show_price (        -- price of each seat tier for a given show
  show_id          BIGINT UNSIGNED  NOT NULL,
  seat_category_id TINYINT UNSIGNED NOT NULL,
  price            DECIMAL(8,2)     NOT NULL,
  PRIMARY KEY (show_id, seat_category_id),
  CONSTRAINT fk_sp_show     FOREIGN KEY (show_id)          REFERENCES movie_show (show_id),
  CONSTRAINT fk_sp_category FOREIGN KEY (seat_category_id) REFERENCES seat_category (seat_category_id),
  CONSTRAINT chk_sp_price CHECK (price >= 0)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- Users, bookings
-- ---------------------------------------------------------------------
CREATE TABLE app_user (
  user_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name       VARCHAR(120) NOT NULL,
  email      VARCHAR(190) NOT NULL,
  phone      VARCHAR(20)  NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id),
  UNIQUE KEY uq_user_email (email),
  UNIQUE KEY uq_user_phone (phone)
) ENGINE=InnoDB;

CREATE TABLE booking (
  booking_id      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id         BIGINT UNSIGNED NOT NULL,
  show_id         BIGINT UNSIGNED NOT NULL,
  status          ENUM('PENDING','CONFIRMED','CANCELLED','EXPIRED') NOT NULL DEFAULT 'PENDING',
  hold_expires_at DATETIME NOT NULL,          -- seat-hold deadline (e.g. now + 10 min)
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  confirmed_at    DATETIME NULL,
  PRIMARY KEY (booking_id),
  KEY idx_booking_sweeper (status, hold_expires_at),   -- expiry sweeper
  KEY idx_booking_user (user_id, created_at),
  KEY idx_booking_show (show_id),
  CONSTRAINT fk_booking_user FOREIGN KEY (user_id) REFERENCES app_user (user_id),
  CONSTRAINT fk_booking_show FOREIGN KEY (show_id) REFERENCES movie_show (show_id)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- show_seat: THE concurrency-control table. One row per (show, seat),
-- materialised when the show is created. Every hold / confirm / release is a
-- compare-and-set on one of these rows, so InnoDB row locks arbitrate races.
-- ---------------------------------------------------------------------
CREATE TABLE show_seat (
  show_seat_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  show_id      BIGINT UNSIGNED NOT NULL,
  seat_id      INT UNSIGNED    NOT NULL,
  status       ENUM('AVAILABLE','HELD','BOOKED') NOT NULL DEFAULT 'AVAILABLE',
  booking_id   BIGINT UNSIGNED NULL,          -- current holder / owner
  version      INT UNSIGNED    NOT NULL DEFAULT 0,   -- optimistic-lock counter
  updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (show_seat_id),
  UNIQUE KEY uq_show_seat (show_id, seat_id),          -- a seat exists once per show
  KEY idx_show_seat_status (show_id, status),          -- seat map / availability counts
  KEY idx_show_seat_booking (booking_id),              -- release-by-booking
  CONSTRAINT fk_ss_show    FOREIGN KEY (show_id)    REFERENCES movie_show (show_id),
  CONSTRAINT fk_ss_seat    FOREIGN KEY (seat_id)    REFERENCES seat (seat_id),
  CONSTRAINT fk_ss_booking FOREIGN KEY (booking_id) REFERENCES booking (booking_id),
  -- DB-level invariant: a seat is unowned iff it is AVAILABLE
  CONSTRAINT chk_ss_owner CHECK (
       (status = 'AVAILABLE' AND booking_id IS NULL)
    OR (status <> 'AVAILABLE' AND booking_id IS NOT NULL))
) ENGINE=InnoDB;

CREATE TABLE booking_seat (      -- immutable line items; survive expiry/cancel for history
  booking_id   BIGINT UNSIGNED NOT NULL,
  show_seat_id BIGINT UNSIGNED NOT NULL,
  price_paid   DECIMAL(8,2)    NOT NULL,     -- price snapshot at hold time
  PRIMARY KEY (booking_id, show_seat_id),
  KEY idx_bs_show_seat (show_seat_id),
  CONSTRAINT fk_bs_booking   FOREIGN KEY (booking_id)   REFERENCES booking (booking_id),
  CONSTRAINT fk_bs_show_seat FOREIGN KEY (show_seat_id) REFERENCES show_seat (show_seat_id)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- Payments + idempotent webhook inbox
-- ---------------------------------------------------------------------
CREATE TABLE payment (
  payment_id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  booking_id       BIGINT UNSIGNED NOT NULL,
  provider         VARCHAR(30)  NOT NULL,     -- 'razorpay', 'stripe' ...
  provider_txn_id  VARCHAR(64)  NOT NULL,     -- gateway order/payment id
  amount           DECIMAL(10,2) NOT NULL,    -- amount actually charged
  status           ENUM('INITIATED','SUCCESS','FAILED','REFUNDED') NOT NULL DEFAULT 'INITIATED',
  created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (payment_id),
  UNIQUE KEY uq_payment_provider_txn (provider, provider_txn_id),
  KEY idx_payment_booking (booking_id),
  CONSTRAINT fk_payment_booking FOREIGN KEY (booking_id) REFERENCES booking (booking_id)
) ENGINE=InnoDB;

CREATE TABLE payment_webhook_event (
  event_id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  provider          VARCHAR(30) NOT NULL,
  provider_event_id VARCHAR(64) NOT NULL,     -- gateway's own unique event id
  provider_txn_id   VARCHAR(64) NOT NULL,
  event_type        VARCHAR(40) NOT NULL,     -- 'payment.captured', 'payment.failed' ...
  payload           JSON NOT NULL,
  received_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  processed_at      DATETIME NULL,
  PRIMARY KEY (event_id),
  -- THE idempotency key: a redelivered event can never be inserted twice
  UNIQUE KEY uq_webhook_provider_event (provider, provider_event_id)
) ENGINE=InnoDB;
