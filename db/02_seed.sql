-- =============================================================================
-- Seed data for the legal_db demo schema. A handful of rows per table, enough
-- to exercise joins across the whole schema.
-- =============================================================================

USE legal_db;

INSERT INTO courts (court_id, name, level, jurisdiction, location) VALUES
    (1, 'Central District Court', 'Trial', 'Federal', 'Springfield'),
    (2, 'State Court of Appeals', 'Appellate', 'State', 'Capital City'),
    (3, 'Supreme Court', 'Supreme', 'Federal', 'Capital City');

INSERT INTO judges (judge_id, full_name, title, court_id, appointed_date) VALUES
    (1, 'Amara Okafor', 'Chief Judge', 1, '2015-03-01'),
    (2, 'David Chen', 'Associate Judge', 1, '2018-09-15'),
    (3, 'Priya Nair', 'Appellate Judge', 2, '2012-01-20'),
    (4, 'Michael Torres', 'Chief Justice', 3, '2008-07-10');

INSERT INTO lawyers (lawyer_id, full_name, bar_number, firm_name) VALUES
    (1, 'Sarah Whitfield', 'BAR-10023', 'Whitfield & Associates'),
    (2, 'James Okonkwo', 'BAR-10456', 'Okonkwo Legal Group'),
    (3, 'Elena Petrova', 'BAR-10789', NULL),
    (4, 'Robert Kim', 'BAR-11002', 'Kim & Partners');

INSERT INTO parties (party_id, name, party_type, contact_info) VALUES
    (1, 'John Marshall', 'Individual', 'john.marshall@example.com'),
    (2, 'Acme Manufacturing Ltd.', 'Organization', 'legal@acme-mfg.example.com'),
    (3, 'Greenfield Holdings', 'Organization', 'contact@greenfield.example.com'),
    (4, 'Lisa Nguyen', 'Individual', 'lisa.nguyen@example.com'),
    (5, 'State Regulatory Authority', 'Organization', 'info@sra.example.gov');

INSERT INTO cases (case_id, case_number, title, court_id, case_type, filing_date, status) VALUES
    (1, 'CV-2023-0142', 'Marshall v. Acme Manufacturing Ltd.', 1, 'Civil - Contract', '2023-02-10', 'Closed'),
    (2, 'CV-2023-0198', 'Greenfield Holdings v. State Regulatory Authority', 1, 'Civil - Regulatory', '2023-04-05', 'On Appeal'),
    (3, 'CR-2022-0087', 'State v. Lisa Nguyen', 1, 'Criminal - Fraud', '2022-11-19', 'Closed'),
    (4, 'AP-2023-0021', 'Greenfield Holdings v. State Regulatory Authority (Appeal)', 2, 'Civil - Regulatory Appeal', '2023-09-01', 'Open'),
    (5, 'CV-2024-0056', 'Acme Manufacturing Ltd. v. John Marshall', 1, 'Civil - Counterclaim', '2024-01-15', 'Open');

INSERT INTO case_parties (case_party_id, case_id, party_id, role, lawyer_id) VALUES
    (1, 1, 1, 'Plaintiff', 1),
    (2, 1, 2, 'Defendant', 4),
    (3, 2, 3, 'Plaintiff', 2),
    (4, 2, 5, 'Defendant', 3),
    (5, 3, 4, 'Defendant', 1),
    (6, 4, 3, 'Appellant', 2),
    (7, 4, 5, 'Respondent', 3),
    (8, 5, 2, 'Plaintiff', 4),
    (9, 5, 1, 'Defendant', 1);

INSERT INTO hearings (hearing_id, case_id, judge_id, hearing_date, hearing_type, outcome_notes) VALUES
    (1, 1, 1, '2023-03-15 09:30:00', 'Preliminary Hearing', 'Motion to dismiss denied.'),
    (2, 1, 1, '2023-06-20 10:00:00', 'Trial', 'Case submitted for judgement.'),
    (3, 2, 2, '2023-05-10 14:00:00', 'Preliminary Hearing', 'Discovery schedule set.'),
    (4, 3, 1, '2022-12-05 09:00:00', 'Arraignment', 'Defendant pleaded not guilty.'),
    (5, 3, 1, '2023-02-14 09:00:00', 'Trial', 'Evidence presented by both sides.'),
    (6, 4, 3, '2023-10-12 11:00:00', 'Appellate Review', 'Briefs accepted, ruling reserved.'),
    (7, 5, 2, '2024-02-20 13:30:00', 'Preliminary Hearing', 'Parties directed to mediation.');

INSERT INTO judgements (judgement_id, case_id, judge_id, decision_date, verdict, summary, full_text_url) VALUES
    (1, 1, 1, '2023-07-01', 'Judgement for Plaintiff', 'The court found that Acme Manufacturing Ltd. breached the supply contract with John Marshall and awarded damages of $85,000.', 'https://records.example.gov/judgements/1'),
    (2, 3, 1, '2023-03-01', 'Guilty', 'The court found Lisa Nguyen guilty of fraud under Section 12 of the Financial Conduct Act and imposed a suspended sentence.', 'https://records.example.gov/judgements/2');

INSERT INTO principles (principle_id, judgement_id, title, description, area_of_law) VALUES
    (1, 1, 'Materiality of Breach', 'A breach is material when it defeats the essential purpose of the contract, entitling the non-breaching party to damages.', 'Contract Law'),
    (2, 1, 'Mitigation of Damages', 'A plaintiff claiming breach of contract must show reasonable steps were taken to mitigate resulting losses.', 'Contract Law'),
    (3, 2, 'Intent in Fraud Cases', 'Establishing fraud requires proof of intentional deception for financial gain, not mere negligence.', 'Criminal Law');

INSERT INTO legislations (legislation_id, title, jurisdiction, enactment_date, status) VALUES
    (1, 'Financial Conduct Act', 'Federal', '2010-06-01', 'In Force'),
    (2, 'Commercial Contracts Act', 'Federal', '1998-01-01', 'Amended'),
    (3, 'Environmental Regulatory Compliance Act', 'Federal', '2015-09-20', 'In Force'),
    (4, 'Consumer Protection Act', 'State', '2005-11-11', 'In Force');

INSERT INTO case_legislation_citations (citation_id, case_id, legislation_id, article_section, citation_context) VALUES
    (1, 1, 2, 'Section 8(2)', 'Cited to establish the standard for material breach of contract.'),
    (2, 2, 3, 'Section 14', 'Cited regarding the scope of regulatory authority over environmental permits.'),
    (3, 3, 1, 'Section 12', 'Cited as the basis for the fraud charge against the defendant.'),
    (4, 4, 3, 'Section 14', 'Cited again on appeal to challenge the interpretation of regulatory scope.');
