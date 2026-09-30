-- =====================================================================
-- P2: all shows on a given date at a given theatre, with show timings
-- Run after 01_schema.sql and 02_seed.sql
-- =====================================================================
USE bookmyshow;

-- Inputs (change these). Theatre 1 = 'PVR Phoenix Marketcity'.
SET @theatre_id = 1;
SET @show_date  = CURDATE();

-- ---------------------------------------------------------------------
-- P2 (main answer): one row per show
-- ---------------------------------------------------------------------
SELECT
    s.show_id,
    m.title                                        AS movie,
    l.name                                         AS language,
    f.name                                         AS format,
    sc.name                                        AS screen,
    s.show_date,
    TIME_FORMAT(s.start_time, '%h:%i %p')          AS show_time,
    TIME_FORMAT(ADDTIME(s.start_time, SEC_TO_TIME(m.duration_minutes * 60)), '%h:%i %p')
                                                   AS ends_at
FROM screen      sc
JOIN movie_show  s ON s.screen_id   = sc.screen_id
JOIN movie       m ON m.movie_id    = s.movie_id
JOIN language    l ON l.language_id = s.language_id
JOIN show_format f ON f.format_id   = s.format_id
WHERE sc.theatre_id = @theatre_id
  AND s.show_date   = @show_date
  AND s.status      = 'SCHEDULED'
ORDER BY m.title, s.start_time;

-- ---------------------------------------------------------------------
-- P2 (UI shape, as in the reference screenshot): one row per movie with
-- all of its timings on that day side by side.
-- ---------------------------------------------------------------------
SELECT
    m.title                                        AS movie,
    l.name                                         AS language,
    f.name                                         AS format,
    GROUP_CONCAT(TIME_FORMAT(s.start_time, '%h:%i %p')
                 ORDER BY s.start_time SEPARATOR ' | ') AS show_timings
FROM screen      sc
JOIN movie_show  s ON s.screen_id   = sc.screen_id
JOIN movie       m ON m.movie_id    = s.movie_id
JOIN language    l ON l.language_id = s.language_id
JOIN show_format f ON f.format_id   = s.format_id
WHERE sc.theatre_id = @theatre_id
  AND s.show_date   = @show_date
  AND s.status      = 'SCHEDULED'
GROUP BY m.movie_id, m.title, l.language_id, l.name, f.format_id, f.name
ORDER BY m.title;

-- ---------------------------------------------------------------------
-- Bonus 1: the "next 7 dates" strip at the top of the theatre page
-- ---------------------------------------------------------------------
SELECT DISTINCT s.show_date
FROM screen sc
JOIN movie_show s ON s.screen_id = sc.screen_id
WHERE sc.theatre_id = @theatre_id
  AND s.show_date BETWEEN CURDATE() AND CURDATE() + INTERVAL 6 DAY
  AND s.status = 'SCHEDULED'
ORDER BY s.show_date;

-- ---------------------------------------------------------------------
-- Bonus 2: same as P2 but with live seats-left per show (drives the
-- green / orange / red colouring of the time chips)
-- ---------------------------------------------------------------------
SELECT
    m.title AS movie,
    TIME_FORMAT(s.start_time, '%h:%i %p') AS show_time,
    (SELECT COUNT(*) FROM show_seat ss
      WHERE ss.show_id = s.show_id AND ss.status = 'AVAILABLE') AS seats_left
FROM screen sc
JOIN movie_show s ON s.screen_id = sc.screen_id
JOIN movie      m ON m.movie_id  = s.movie_id
WHERE sc.theatre_id = @theatre_id
  AND s.show_date   = @show_date
  AND s.status      = 'SCHEDULED'
ORDER BY m.title, s.start_time;
