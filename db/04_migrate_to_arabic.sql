-- =============================================================================
-- Migrate an existing English legal_db to Arabic.
--
-- For a database that already has the English demo data loaded. A fresh install
-- needs neither this file nor two steps — 01_schema.sql and 02_seed.sql are
-- already Arabic.
--
-- Run ONCE with an admin user (the application user texttosql_ro has no write
-- grants by design), then load the Arabic seed:
--
--   mysql -h <host> -u <admin> -p --default-character-set=utf8mb4 < db/04_migrate_to_arabic.sql
--   mysql -h <host> -u <admin> -p --default-character-set=utf8mb4 < db/02_seed.sql
--
-- The --default-character-set=utf8mb4 flag matters. Without it the client can
-- negotiate a legacy charset and every Arabic literal is stored mangled.
--
-- This file only changes structure and clears rows; all Arabic data lives in
-- 02_seed.sql so there is exactly one copy of it.
--
-- NOT idempotent: MySQL has no ADD COLUMN IF NOT EXISTS, so re-running fails on
-- section 3. To redo it, drop the *_norm columns first.
-- =============================================================================

USE legal_db;

-- -----------------------------------------------------------------------------
-- 1. Clear existing rows, children before parents so no FK is ever violated.
-- -----------------------------------------------------------------------------
DELETE FROM case_legislation_citations;
DELETE FROM principles;
DELETE FROM judgements;
DELETE FROM hearings;
DELETE FROM case_parties;
DELETE FROM cases;
DELETE FROM parties;
DELETE FROM lawyers;
DELETE FROM judges;
DELETE FROM legislations;
DELETE FROM courts;

-- -----------------------------------------------------------------------------
-- 2. Translate the ENUM definitions.
--
-- Done while the tables are empty, so there is no value conversion to handle.
-- These values are what the LLM must reproduce verbatim in a WHERE clause, so
-- they are also listed in ingestion/schema_docs.py — the two must stay in step
-- or generated queries will filter on values that do not exist.
-- -----------------------------------------------------------------------------
ALTER TABLE courts
    MODIFY COLUMN level ENUM('ابتدائية', 'استئناف', 'عليا') NOT NULL;

ALTER TABLE parties
    MODIFY COLUMN party_type ENUM('شخص طبيعي', 'جهة اعتبارية') NOT NULL;

ALTER TABLE cases
    MODIFY COLUMN status ENUM('مفتوحة', 'مغلقة', 'قيد الاستئناف', 'مشطوبة')
        NOT NULL DEFAULT 'مفتوحة';

ALTER TABLE case_parties
    MODIFY COLUMN role ENUM('مدعي', 'مدعى عليه', 'مستأنف', 'مستأنف ضده', 'طرف ثالث') NOT NULL;

ALTER TABLE legislations
    MODIFY COLUMN status ENUM('ساري', 'ملغى', 'معدل') NOT NULL DEFAULT 'ساري';

-- -----------------------------------------------------------------------------
-- 3. Normalized search columns.
--
-- This is the part that matters for correctness rather than cosmetics. MySQL
-- folds Arabic diacritics but NOT letter variants: 'أحمد' does not match
-- 'احمد' under any collation (verified on this server against
-- utf8mb4_unicode_ci, utf8mb4_0900_ai_ci and utf8mb4_general_ci). Without a
-- normalized column, a user who spells a name a slightly different way gets
-- zero rows and a confident "no results".
--
-- Folding applied (matches Lucene's ArabicNormalizationFilter):
--     أ إ آ ٱ  -> ا      hamza/madda carriers collapse to bare alef
--     ة        -> ه      taa marbuta
--     ى        -> ي      alef maqsura
--     ـ        -> (gone) tatweel / kashida
--     harakat  -> (gone) all diacritics
--
-- STORED rather than VIRTUAL so the column can be indexed. A LIKE '%...%'
-- cannot use a B-tree index, but the index still pays for exact and prefix
-- lookups, and STORED avoids re-evaluating the expression per row.
--
-- If your server rejects REGEXP_REPLACE inside a generated column, drop the
-- REGEXP_REPLACE wrapper and keep the REPLACE chain: utf8mb4_unicode_ci already
-- ignores diacritics when comparing, so letter folding is the part that has to
-- be materialized.
-- -----------------------------------------------------------------------------
ALTER TABLE judges
    ADD COLUMN full_name_norm VARCHAR(255)
        GENERATED ALWAYS AS (
            REGEXP_REPLACE(
                REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                    LOWER(TRIM(full_name)),
                    'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ٱ', 'ا'),
                    'ة', 'ه'), 'ى', 'ي'), 'ـ', ''),
                '[ًٌٍَُِّْٰٕٓٔ]', '')
        ) STORED,
    ADD INDEX idx_judges_full_name_norm (full_name_norm);

ALTER TABLE lawyers
    ADD COLUMN full_name_norm VARCHAR(255)
        GENERATED ALWAYS AS (
            REGEXP_REPLACE(
                REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                    LOWER(TRIM(full_name)),
                    'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ٱ', 'ا'),
                    'ة', 'ه'), 'ى', 'ي'), 'ـ', ''),
                '[ًٌٍَُِّْٰٕٓٔ]', '')
        ) STORED,
    ADD INDEX idx_lawyers_full_name_norm (full_name_norm);

ALTER TABLE parties
    ADD COLUMN name_norm VARCHAR(255)
        GENERATED ALWAYS AS (
            REGEXP_REPLACE(
                REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                    LOWER(TRIM(name)),
                    'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ٱ', 'ا'),
                    'ة', 'ه'), 'ى', 'ي'), 'ـ', ''),
                '[ًٌٍَُِّْٰٕٓٔ]', '')
        ) STORED,
    ADD INDEX idx_parties_name_norm (name_norm);

ALTER TABLE cases
    ADD COLUMN title_norm VARCHAR(500)
        GENERATED ALWAYS AS (
            REGEXP_REPLACE(
                REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                    LOWER(TRIM(title)),
                    'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ٱ', 'ا'),
                    'ة', 'ه'), 'ى', 'ي'), 'ـ', ''),
                '[ًٌٍَُِّْٰٕٓٔ]', '')
        ) STORED,
    ADD INDEX idx_cases_title_norm (title_norm);

ALTER TABLE legislations
    ADD COLUMN title_norm VARCHAR(255)
        GENERATED ALWAYS AS (
            REGEXP_REPLACE(
                REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                    LOWER(TRIM(title)),
                    'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ٱ', 'ا'),
                    'ة', 'ه'), 'ى', 'ي'), 'ـ', ''),
                '[ًٌٍَُِّْٰٕٓٔ]', '')
        ) STORED,
    ADD INDEX idx_legislations_title_norm (title_norm);

ALTER TABLE principles
    ADD COLUMN title_norm VARCHAR(255)
        GENERATED ALWAYS AS (
            REGEXP_REPLACE(
                REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                    LOWER(TRIM(title)),
                    'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ٱ', 'ا'),
                    'ة', 'ه'), 'ى', 'ي'), 'ـ', ''),
                '[ًٌٍَُِّْٰٕٓٔ]', '')
        ) STORED,
    ADD INDEX idx_principles_title_norm (title_norm);

-- -----------------------------------------------------------------------------
-- Next: load db/02_seed.sql, then re-run `python ingestion/ingest.py` so the
-- schema documents in Qdrant describe the Arabic ENUM values and the new _norm
-- columns. Until you re-ingest, the Schema updates tab will report the drift —
-- which is a fair way to check that tab actually works.
--
-- Sanity check once seeded (both spellings must return the same judge):
--   SELECT full_name FROM judges WHERE full_name_norm LIKE '%امل%';
--   SELECT full_name FROM judges WHERE full_name_norm LIKE '%عبد الرحمن%';
-- -----------------------------------------------------------------------------
