-- =============================================================================
-- Application state: chat conversations and their turns.
--
-- This is the FIRST thing the backend writes to MySQL. Everything before it —
-- the query pipeline, the catalog, the concept sync — either reads `legal_db`
-- or keeps its state in Qdrant, Redis or a file under data/.
--
-- WHY A SEPARATE DATABASE, NOT A COUPLE OF TABLES IN legal_db
-- -----------------------------------------------------------
-- Two mechanisms would otherwise pick these tables up, and both are working as
-- designed:
--
--   1. Debezium's include list is MYSQL_DATABASE (docker-compose.yml), i.e.
--      legal_db. A CREATE TABLE there fires a schema-change event, the CDC path
--      documents the table, the reviewer passes it, and it is embedded into
--      Qdrant as something the retriever may return.
--
--   2. texttosql_ro holds SELECT on legal_db.* — deliberately all of it, so a
--      newly documented table is queryable without a grant change.
--
-- Put the transcripts in legal_db and the consequence is not subtle: the model
-- can write SELECTs against the chat history. "ما هي أكثر الأسئلة تكرارًا؟"
-- would return a real answer, assembled out of what other people asked. The
-- corpus the system answers FROM has to stay separate from the record of what it
-- was ASKED.
--
-- app_db is outside Debezium's include list and outside texttosql_ro's grants,
-- so the separation is enforced by configuration and privilege rather than by
-- everyone remembering. Do not add app_db to DEBEZIUM_SOURCE_DATABASE_INCLUDE_LIST,
-- and do not grant texttosql_ro anything here.
--
-- Usage:
--   mysql -h <host> -P <port> -u <admin_user> -p --default-character-set=utf8mb4 < db/07_app_schema.sql
--
-- Edit the password before running in any shared/non-local environment.
-- =============================================================================

CREATE DATABASE IF NOT EXISTS app_db
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE app_db;

-- -----------------------------------------------------------------------------
-- chats: one conversation, with its own context window
--
-- The window is not stored. It is recomputed per request from this chat's turns
-- (backend/app/services/context.py), which is what makes "each chat has its own
-- memory" true by construction rather than by bookkeeping: two chats cannot
-- share a budget when the budget is derived from the rows.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chats (
    -- CHAR(36) UUID rather than AUTO_INCREMENT: the browser creates a chat and
    -- starts streaming into it, and a client-generated id means it does not have
    -- to wait for a round trip to know what to key its local state on.
    id          CHAR(36) PRIMARY KEY,
    -- Written from the first question, truncated. NULL until that turn lands, so
    -- an empty chat can be told apart from one genuinely titled "".
    title       VARCHAR(255) NULL,
    created_at  DATETIME(3) NOT NULL,
    -- Bumped on every appended turn. This is the sidebar's sort key — chats are
    -- listed by last activity, not by creation, so the one you were just in is
    -- at the top.
    updated_at  DATETIME(3) NOT NULL,
    INDEX idx_chats_updated (updated_at DESC)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- turns: one completed exchange
--
-- Columns mirror HistoryTurn in backend/app/models.py exactly, so replaying a
-- conversation to the model needs no translation step. Keep them in step.
--
-- RESULT ROWS ARE NOT STORED, for the same reason they are not replayed: one
-- 200-row result outweighs the schema, the system prompt and the rest of the
-- transcript combined, and `answer` already says what the rows showed. A turn
-- is re-executable from `sql_text` if the numbers are wanted again.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS turns (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    chat_id     CHAR(36) NOT NULL,
    question    TEXT NOT NULL,
    -- `sql` is a reserved word in MySQL and would need back-ticking at every
    -- use site. Named for the storage, mapped back to HistoryTurn.sql in
    -- services/chat_store.py.
    sql_text    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    -- The tables this turn resolved to, carried forward so a follow-up that
    -- retrieves nothing on its own still sees the schema its predecessor's SQL
    -- references. A JSON array of names, not a child table: nothing queries by
    -- table name, and a join per turn to read a list of five strings buys
    -- correctness nobody spends.
    table_names JSON NOT NULL,
    created_at  DATETIME(3) NOT NULL,
    -- Deleting a chat deletes its turns. There is no soft delete: "delete this
    -- conversation" from a user means the transcript is gone, and a row that
    -- survives it is a surprise nobody asked for.
    CONSTRAINT fk_turns_chat FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
    -- Covers both reads that matter: replay (all turns of one chat, in order)
    -- and the turn count behind the context meter. `id` ascending IS insertion
    -- order, so history needs no sort on created_at.
    INDEX idx_turns_chat (chat_id, id)
) ENGINE=InnoDB;

-- =============================================================================
-- The application's write user.
--
-- Separate from texttosql_ro because that account executes MODEL-GENERATED SQL.
-- Giving it INSERT anywhere would mean a bug in sql_guard.py is a write bug, and
-- the whole point of 03_readonly_user.sql is that the guard is not the thing
-- standing between a hallucinated statement and the data.
--
-- This account never executes model output. Every statement it runs is written
-- by hand in services/chat_store.py and fully parameterized.
-- =============================================================================

CREATE USER IF NOT EXISTS 'texttosql_app'@'%' IDENTIFIED BY 'password';

-- DML on app_db only. No DDL: the schema above is applied by an admin running
-- this file, so the application has no reason to be able to alter it, and a
-- migration is a deliberate act rather than something a code path can do.
GRANT SELECT, INSERT, UPDATE, DELETE ON app_db.* TO 'texttosql_app'@'%';

-- Grant, revoke, re-grant — the same order as 03_readonly_user.sql, and the
-- order matters. `REVOKE ALL PRIVILEGES, GRANT OPTION` against an account that
-- holds nothing raises "There is no such grant defined", which would abort this
-- script on a first run. Granting first guarantees there is something to revoke,
-- so the defensive clear-out works on a fresh install and on a re-run alike.
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'texttosql_app'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON app_db.* TO 'texttosql_app'@'%';

FLUSH PRIVILEGES;

-- --------------------------------------------------------------------------
-- Verify the isolation actually holds. Both of these should return ZERO rows;
-- if either returns anything, the separation this file exists for is broken:
--
--   -- texttosql_ro must have no privilege on app_db
--   SELECT * FROM information_schema.schema_privileges
--    WHERE grantee LIKE "'texttosql_ro'%" AND table_schema = 'app_db';
--
--   -- texttosql_app must have no privilege on legal_db
--   SELECT * FROM information_schema.schema_privileges
--    WHERE grantee LIKE "'texttosql_app'%" AND table_schema = 'legal_db';
-- --------------------------------------------------------------------------
