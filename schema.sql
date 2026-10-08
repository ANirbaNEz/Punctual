-- Punctual attendance schema (MySQL 5.7+ / 8.x / MariaDB 10.3+)
-- Run once:  mysql -u root -p < schema.sql
-- The admin account is created automatically by main.py on first start.
-- Already have the tables from an earlier version? Run only the "Upgrade an existing database" block at the bottom.

CREATE DATABASE IF NOT EXISTS attendance
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE attendance;

-- Admins and employees. New self-registered accounts start as 'pending' until an admin approves them.
CREATE TABLE IF NOT EXISTS users (
  id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  name        VARCHAR(60)  NOT NULL,
  username    VARCHAR(64)  NOT NULL,
  pw          VARCHAR(200) NOT NULL,                       -- salt:scrypt hash, never the password
  role        ENUM('admin','employee') NOT NULL DEFAULT 'employee',
  status      ENUM('pending','active','disabled') NOT NULL DEFAULT 'pending',
  shift       CHAR(5)      NOT NULL DEFAULT '09:00',       -- HH:MM shift start, per employee
  face_emb    TEXT         NULL,                           -- 128-number face signature (JSON), not the photo
  face_photo  VARCHAR(64)  NULL,                           -- file name of the enrolment selfie
  face_status ENUM('none','pending','approved') NOT NULL DEFAULT 'none',
  created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_users_username (username)
) ENGINE=InnoDB;

-- Key/value rules: office_lat, office_lng, radius (m), grace (min), secret (JWT signing key).
CREATE TABLE IF NOT EXISTS settings (
  k VARCHAR(40)  NOT NULL PRIMARY KEY,
  v VARCHAR(255) NOT NULL
) ENGINE=InnoDB;

-- One row per employee per day. check_in / check_out are real date-times (server local time).
CREATE TABLE IF NOT EXISTS attendance (
  id        INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  user_id   INT UNSIGNED NOT NULL,
  `day`     DATE         NOT NULL,
  check_in  DATETIME     NOT NULL,
  in_lat    DOUBLE       NOT NULL,
  in_lng    DOUBLE       NOT NULL,
  in_acc    DOUBLE       NULL,                            -- GPS accuracy in metres
  in_addr   TEXT         NULL,                            -- reverse-geocoded address (OpenStreetMap)
  in_photo  VARCHAR(64)  NULL,                            -- selfie taken at check-in
  in_face   DOUBLE       NULL,                            -- face match score (higher = closer)
  status    ENUM('early','on_time','late') NOT NULL,
  late_min  INT          NOT NULL DEFAULT 0,
  check_out DATETIME     NULL,
  out_lat   DOUBLE       NULL,
  out_lng   DOUBLE       NULL,
  out_addr  TEXT         NULL,
  out_photo VARCHAR(64)  NULL,
  out_face  DOUBLE       NULL,
  UNIQUE KEY uq_attendance_user_day (user_id, `day`),
  KEY ix_attendance_day (`day`),
  CONSTRAINT fk_attendance_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------------
-- Upgrade an existing database (tables created before selfie / face match).
-- Run these two statements once, on the `attendance` database:
--
-- ALTER TABLE users
--   ADD COLUMN face_emb TEXT NULL,
--   ADD COLUMN face_photo VARCHAR(64) NULL,
--   ADD COLUMN face_status ENUM('none','pending','approved') NOT NULL DEFAULT 'none';
--
-- ALTER TABLE attendance
--   ADD COLUMN in_photo VARCHAR(64) NULL,
--   ADD COLUMN in_face DOUBLE NULL,
--   ADD COLUMN out_photo VARCHAR(64) NULL,
--   ADD COLUMN out_face DOUBLE NULL;
-- ---------------------------------------------------------------------------
