-- Replication user for Debezium change capture.
--
-- The query pipeline's `texttosql_ro` (03_readonly_user.sql) deliberately holds
-- SELECT and nothing else. Reading the binary log needs privileges that are
-- server-wide rather than schema-scoped, and granting those to the account that
-- executes model-generated SQL would widen its blast radius for no reason. So
-- change capture gets its own account.
--
-- Run as root/admin on the MySQL instance the backend points at.

CREATE USER IF NOT EXISTS 'debezium'@'%' IDENTIFIED BY 'change_me_cdc_password';

-- REPLICATION SLAVE  — read the binary log stream itself.
-- REPLICATION CLIENT — read SHOW MASTER STATUS, to find the starting position.
-- RELOAD             — FLUSH TABLES WITH READ LOCK during the initial snapshot.
-- SELECT / SHOW      — read table structure while building the schema history.
--
-- The first two are server-wide because the binary log is a server-wide object;
-- MySQL has no way to scope them to one schema.
GRANT SELECT, RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT
  ON *.* TO 'debezium'@'%';

FLUSH PRIVILEGES;

-- --------------------------------------------------------------------------
-- Server prerequisites
--
-- Debezium cannot start without these. Check them first:
--
--   SELECT @@log_bin, @@binlog_format, @@binlog_row_image, @@server_id;
--
-- Expected: 1, ROW, FULL, and any non-zero server id.
--
-- If log_bin is 0 the binary log is off and no amount of granting fixes it —
-- it is set in my.cnf / my.ini and needs a server RESTART:
--
--   [mysqld]
--   log_bin           = mysql-bin
--   binlog_format     = ROW
--   binlog_row_image  = FULL
--   server_id         = 1
--   expire_logs_days  = 7
--
-- binlog_format must be ROW. STATEMENT and MIXED record the SQL text rather
-- than the resulting row images, and Debezium cannot reconstruct changes from
-- them — it fails at startup rather than silently degrading.
-- --------------------------------------------------------------------------
