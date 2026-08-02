-- =============================================================================
-- Read-only MySQL user for the text-to-SQL backend.
--
-- This is the real guardrail against DDL/DML — the application-level SQL
-- guard (backend/app/services/sql_guard.py) is a fast-fail defense in depth,
-- not the source of truth. Even if the guard is buggy or bypassed, this user
-- physically cannot write to or alter the schema.
--
-- Edit the password before running in any shared/non-local environment.
--
-- Usage:
--   mysql -h <host> -P <port> -u <admin_user> -p < db/03_readonly_user.sql
-- =============================================================================

CREATE USER IF NOT EXISTS 'texttosql_ro'@'%' IDENTIFIED BY 'change_me_readonly_password';

-- SELECT only, on the demo schema only. No INSERT/UPDATE/DELETE/DDL grants at all.
GRANT SELECT ON legal_db.* TO 'texttosql_ro'@'%';

-- Defensive: explicitly revoke anything broader that might exist from a prior grant.
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'texttosql_ro'@'%';
GRANT SELECT ON legal_db.* TO 'texttosql_ro'@'%';

FLUSH PRIVILEGES;
