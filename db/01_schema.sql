-- =============================================================================
-- Text-to-SQL demo schema — legal domain (Arabic)
-- =============================================================================
-- Run against an existing MySQL instance. Creates the database (if missing)
-- and all tables for the demo. Safe to re-run: uses CREATE TABLE IF NOT EXISTS.
--
-- Usage:
--   mysql -h <host> -P <port> -u <admin_user> -p --default-character-set=utf8mb4 < db/01_schema.sql
--
-- Language: identifiers (tables, columns, foreign keys) are English by design —
-- the user never sees them and the LLM writes better SQL against them. Stored
-- VALUES are Arabic, including every ENUM. See ingestion/schema_docs.py, which
-- must list the same ENUM values so generated queries filter on literals that
-- actually exist.
--
-- Every name/title column carries a generated `_norm` twin. MySQL folds Arabic
-- diacritics but NOT letter variants — 'أحمد' does not match 'احمد' under any
-- collation — so matching has to run against the normalized column or users who
-- spell a name differently silently get zero rows. Folding applied:
--     أ إ آ ٱ -> ا     ة -> ه     ى -> ي     tatweel and harakat removed
-- (the same set as Lucene's ArabicNormalizationFilter).
--
-- To convert an already-populated ENGLISH database instead, see
-- db/04_migrate_to_arabic.sql.
-- =============================================================================

CREATE DATABASE IF NOT EXISTS legal_db
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE legal_db;

-- -----------------------------------------------------------------------------
-- courts: judicial bodies that hear cases
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS courts (
    court_id      INT AUTO_INCREMENT PRIMARY KEY,
    name          VARCHAR(255) NOT NULL,
    level         ENUM('ابتدائية', 'استئناف', 'عليا') NOT NULL,
    jurisdiction  VARCHAR(255) NOT NULL,
    location      VARCHAR(255) NOT NULL
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- judges: judicial officers presiding over cases, attached to a home court
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS judges (
    judge_id      INT AUTO_INCREMENT PRIMARY KEY,
    full_name     VARCHAR(255) NOT NULL,
    title         VARCHAR(100) NOT NULL,
    court_id      INT NOT NULL,
    appointed_date DATE NULL,
    full_name_norm VARCHAR(255) GENERATED ALWAYS AS (
        REGEXP_REPLACE(
            REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                LOWER(TRIM(full_name)),
                'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ٱ', 'ا'),
                'ة', 'ه'), 'ى', 'ي'), 'ـ', ''),
            '[ًٌٍَُِّْٰٕٓٔ]', '')
    ) STORED,
    CONSTRAINT fk_judges_court FOREIGN KEY (court_id) REFERENCES courts(court_id),
    INDEX idx_judges_full_name_norm (full_name_norm)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- lawyers: legal counsel who represent parties in cases
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lawyers (
    lawyer_id     INT AUTO_INCREMENT PRIMARY KEY,
    full_name     VARCHAR(255) NOT NULL,
    bar_number    VARCHAR(50) NOT NULL UNIQUE,
    firm_name     VARCHAR(255) NULL,
    full_name_norm VARCHAR(255) GENERATED ALWAYS AS (
        REGEXP_REPLACE(
            REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                LOWER(TRIM(full_name)),
                'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ٱ', 'ا'),
                'ة', 'ه'), 'ى', 'ي'), 'ـ', ''),
            '[ًٌٍَُِّْٰٕٓٔ]', '')
    ) STORED,
    INDEX idx_lawyers_full_name_norm (full_name_norm)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- parties: individuals or organizations involved in a case (plaintiff, etc.)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS parties (
    party_id      INT AUTO_INCREMENT PRIMARY KEY,
    name          VARCHAR(255) NOT NULL,
    party_type    ENUM('شخص طبيعي', 'جهة اعتبارية') NOT NULL,
    contact_info  VARCHAR(255) NULL,
    name_norm     VARCHAR(255) GENERATED ALWAYS AS (
        REGEXP_REPLACE(
            REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                LOWER(TRIM(name)),
                'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ٱ', 'ا'),
                'ة', 'ه'), 'ى', 'ي'), 'ـ', ''),
            '[ًٌٍَُِّْٰٕٓٔ]', '')
    ) STORED,
    INDEX idx_parties_name_norm (name_norm)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- cases: the central case record, filed at a specific court
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cases (
    case_id       INT AUTO_INCREMENT PRIMARY KEY,
    case_number   VARCHAR(50) NOT NULL UNIQUE,
    title         VARCHAR(500) NOT NULL,
    court_id      INT NOT NULL,
    case_type     VARCHAR(100) NOT NULL,
    filing_date   DATE NOT NULL,
    status        ENUM('مفتوحة', 'مغلقة', 'قيد الاستئناف', 'مشطوبة') NOT NULL DEFAULT 'مفتوحة',
    title_norm    VARCHAR(500) GENERATED ALWAYS AS (
        REGEXP_REPLACE(
            REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                LOWER(TRIM(title)),
                'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ٱ', 'ا'),
                'ة', 'ه'), 'ى', 'ي'), 'ـ', ''),
            '[ًٌٍَُِّْٰٕٓٔ]', '')
    ) STORED,
    CONSTRAINT fk_cases_court FOREIGN KEY (court_id) REFERENCES courts(court_id),
    INDEX idx_cases_title_norm (title_norm)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- case_parties: links parties to a case with their role, optionally
-- represented by a lawyer (many-to-many resolver between cases and parties)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS case_parties (
    case_party_id INT AUTO_INCREMENT PRIMARY KEY,
    case_id       INT NOT NULL,
    party_id      INT NOT NULL,
    role          ENUM('مدعي', 'مدعى عليه', 'مستأنف', 'مستأنف ضده', 'طرف ثالث') NOT NULL,
    lawyer_id     INT NULL,
    CONSTRAINT fk_cp_case   FOREIGN KEY (case_id) REFERENCES cases(case_id),
    CONSTRAINT fk_cp_party  FOREIGN KEY (party_id) REFERENCES parties(party_id),
    CONSTRAINT fk_cp_lawyer FOREIGN KEY (lawyer_id) REFERENCES lawyers(lawyer_id)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- hearings: scheduled court sessions for a case, presided over by a judge
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hearings (
    hearing_id    INT AUTO_INCREMENT PRIMARY KEY,
    case_id       INT NOT NULL,
    judge_id      INT NOT NULL,
    hearing_date  DATETIME NOT NULL,
    hearing_type  VARCHAR(100) NOT NULL,
    outcome_notes TEXT NULL,
    CONSTRAINT fk_hearings_case  FOREIGN KEY (case_id) REFERENCES cases(case_id),
    CONSTRAINT fk_hearings_judge FOREIGN KEY (judge_id) REFERENCES judges(judge_id)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- judgements: final rulings issued for a case by a judge
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS judgements (
    judgement_id  INT AUTO_INCREMENT PRIMARY KEY,
    case_id       INT NOT NULL,
    judge_id      INT NOT NULL,
    decision_date DATE NOT NULL,
    verdict       VARCHAR(255) NOT NULL,
    summary       TEXT NOT NULL,
    full_text_url VARCHAR(500) NULL,
    CONSTRAINT fk_judgements_case  FOREIGN KEY (case_id) REFERENCES cases(case_id),
    CONSTRAINT fk_judgements_judge FOREIGN KEY (judge_id) REFERENCES judges(judge_id)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- principles: legal principles established or applied in a judgement
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS principles (
    principle_id  INT AUTO_INCREMENT PRIMARY KEY,
    judgement_id  INT NOT NULL,
    title         VARCHAR(255) NOT NULL,
    description   TEXT NOT NULL,
    area_of_law   VARCHAR(100) NOT NULL,
    title_norm    VARCHAR(255) GENERATED ALWAYS AS (
        REGEXP_REPLACE(
            REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                LOWER(TRIM(title)),
                'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ٱ', 'ا'),
                'ة', 'ه'), 'ى', 'ي'), 'ـ', ''),
            '[ًٌٍَُِّْٰٕٓٔ]', '')
    ) STORED,
    CONSTRAINT fk_principles_judgement FOREIGN KEY (judgement_id) REFERENCES judgements(judgement_id),
    INDEX idx_principles_title_norm (title_norm)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- legislations: statutes and acts that cases may cite
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS legislations (
    legislation_id  INT AUTO_INCREMENT PRIMARY KEY,
    title           VARCHAR(255) NOT NULL,
    jurisdiction    VARCHAR(255) NOT NULL,
    enactment_date  DATE NOT NULL,
    status          ENUM('ساري', 'ملغى', 'معدل') NOT NULL DEFAULT 'ساري',
    title_norm      VARCHAR(255) GENERATED ALWAYS AS (
        REGEXP_REPLACE(
            REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                LOWER(TRIM(title)),
                'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ٱ', 'ا'),
                'ة', 'ه'), 'ى', 'ي'), 'ـ', ''),
            '[ًٌٍَُِّْٰٕٓٔ]', '')
    ) STORED,
    INDEX idx_legislations_title_norm (title_norm)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- case_legislation_citations: links cases to the legislations they cite
-- (many-to-many resolver between cases and legislations)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS case_legislation_citations (
    citation_id      INT AUTO_INCREMENT PRIMARY KEY,
    case_id          INT NOT NULL,
    legislation_id   INT NOT NULL,
    article_section   VARCHAR(100) NULL,
    citation_context TEXT NULL,
    CONSTRAINT fk_clc_case        FOREIGN KEY (case_id) REFERENCES cases(case_id),
    CONSTRAINT fk_clc_legislation FOREIGN KEY (legislation_id) REFERENCES legislations(legislation_id)
) ENGINE=InnoDB;
