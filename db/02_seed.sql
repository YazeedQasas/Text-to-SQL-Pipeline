-- =============================================================================
-- Seed data for the legal_db demo schema (Arabic). A handful of rows per table,
-- enough to exercise joins across the whole schema.
--
-- Run with an explicit charset or Arabic literals may be stored mangled:
--   mysql -h <host> -u <admin> -p --default-character-set=utf8mb4 < db/02_seed.sql
--
-- Reference codes — case_number, bar_number, URLs, e-mail addresses — stay in
-- Latin script on purpose. They are identifiers a user copies verbatim out of a
-- record, not prose, and the answer prompt reproduces them unchanged.
--
-- ENUM values here must match the definitions in 01_schema.sql exactly, and are
-- mirrored again in ingestion/schema_docs.py so the LLM knows which literal to
-- filter on.
-- =============================================================================

USE legal_db;

INSERT INTO courts (court_id, name, level, jurisdiction, location) VALUES
    (1, 'المحكمة الابتدائية المركزية', 'ابتدائية', 'اتحادي', 'عمّان'),
    (2, 'محكمة الاستئناف', 'استئناف', 'محلي', 'إربد'),
    (3, 'المحكمة العليا', 'عليا', 'اتحادي', 'عمّان');

INSERT INTO judges (judge_id, full_name, title, court_id, appointed_date) VALUES
    (1, 'أمل عبد الرحمن', 'رئيسة المحكمة', 1, '2015-03-01'),
    (2, 'داود شاهين', 'قاضٍ مشارك', 1, '2018-09-15'),
    (3, 'نور الهاشمي', 'قاضية استئناف', 2, '2012-01-20'),
    (4, 'سليم الطراونة', 'رئيس القضاة', 3, '2008-07-10');

INSERT INTO lawyers (lawyer_id, full_name, bar_number, firm_name) VALUES
    (1, 'سارة الخطيب', 'BAR-10023', 'مكتب الخطيب وشركاه'),
    (2, 'جميل عبد الله', 'BAR-10456', 'مجموعة عبد الله القانونية'),
    (3, 'إيمان درويش', 'BAR-10789', NULL),
    (4, 'رامي قاسم', 'BAR-11002', 'قاسم وشركاؤه');

INSERT INTO parties (party_id, name, party_type, contact_info) VALUES
    (1, 'يوسف المرشد', 'شخص طبيعي', 'yousef.murshid@example.com'),
    (2, 'شركة القمة للصناعات المحدودة', 'جهة اعتبارية', 'legal@alqimma.example.com'),
    (3, 'مجموعة الحقول الخضراء القابضة', 'جهة اعتبارية', 'contact@greenfields.example.com'),
    (4, 'ليلى النجار', 'شخص طبيعي', 'laila.najjar@example.com'),
    (5, 'هيئة التنظيم الحكومية', 'جهة اعتبارية', 'info@regulator.example.gov');

INSERT INTO cases (case_id, case_number, title, court_id, case_type, filing_date, status) VALUES
    (1, 'CV-2023-0142', 'المرشد ضد شركة القمة للصناعات المحدودة', 1, 'مدني - عقود', '2023-02-10', 'مغلقة'),
    (2, 'CV-2023-0198', 'مجموعة الحقول الخضراء القابضة ضد هيئة التنظيم الحكومية', 1, 'مدني - تنظيمي', '2023-04-05', 'قيد الاستئناف'),
    (3, 'CR-2022-0087', 'النيابة العامة ضد ليلى النجار', 1, 'جنائي - احتيال', '2022-11-19', 'مغلقة'),
    (4, 'AP-2023-0021', 'مجموعة الحقول الخضراء القابضة ضد هيئة التنظيم الحكومية (استئناف)', 2, 'مدني - استئناف تنظيمي', '2023-09-01', 'مفتوحة'),
    (5, 'CV-2024-0056', 'شركة القمة للصناعات المحدودة ضد يوسف المرشد', 1, 'مدني - دعوى متقابلة', '2024-01-15', 'مفتوحة');

INSERT INTO case_parties (case_party_id, case_id, party_id, role, lawyer_id) VALUES
    (1, 1, 1, 'مدعي', 1),
    (2, 1, 2, 'مدعى عليه', 4),
    (3, 2, 3, 'مدعي', 2),
    (4, 2, 5, 'مدعى عليه', 3),
    (5, 3, 4, 'مدعى عليه', 1),
    (6, 4, 3, 'مستأنف', 2),
    (7, 4, 5, 'مستأنف ضده', 3),
    (8, 5, 2, 'مدعي', 4),
    (9, 5, 1, 'مدعى عليه', 1);

INSERT INTO hearings (hearing_id, case_id, judge_id, hearing_date, hearing_type, outcome_notes) VALUES
    (1, 1, 1, '2023-03-15 09:30:00', 'جلسة تمهيدية', 'رُدّ طلب رد الدعوى.'),
    (2, 1, 1, '2023-06-20 10:00:00', 'محاكمة', 'حُجزت القضية للحكم.'),
    (3, 2, 2, '2023-05-10 14:00:00', 'جلسة تمهيدية', 'حُدّد جدول تبادل البيّنات.'),
    (4, 3, 1, '2022-12-05 09:00:00', 'جلسة اتهام', 'أنكرت المدعى عليها التهمة المنسوبة إليها.'),
    (5, 3, 1, '2023-02-14 09:00:00', 'محاكمة', 'قدّم الطرفان بيّناتهما.'),
    (6, 4, 3, '2023-10-12 11:00:00', 'مراجعة استئنافية', 'قُبلت المذكرات وحُجز القرار.'),
    (7, 5, 2, '2024-02-20 13:30:00', 'جلسة تمهيدية', 'أُحيل الطرفان إلى الوساطة.');

INSERT INTO judgements (judgement_id, case_id, judge_id, decision_date, verdict, summary, full_text_url) VALUES
    (1, 1, 1, '2023-07-01', 'الحكم لصالح المدعي',
     'وجدت المحكمة أن شركة القمة للصناعات المحدودة أخلّت بعقد التوريد المبرم مع يوسف المرشد، وقضت بتعويض قدره 85,000 دينار.',
     'https://records.example.gov/judgements/1'),
    (2, 3, 1, '2023-03-01', 'مدان',
     'وجدت المحكمة ليلى النجار مدانة بالاحتيال وفق المادة 12 من قانون السلوك المالي، وقضت بحبسها مع وقف التنفيذ.',
     'https://records.example.gov/judgements/2');

INSERT INTO principles (principle_id, judgement_id, title, description, area_of_law) VALUES
    (1, 1, 'جسامة الإخلال',
     'يكون الإخلال جسيمًا متى أفقد العقد غرضه الأساسي، وهو ما يخوّل الطرف المتضرر المطالبة بالتعويض.',
     'قانون العقود'),
    (2, 1, 'التخفيف من الضرر',
     'على المدعي بالإخلال بالعقد أن يثبت اتخاذه خطوات معقولة للحد من الخسائر الناتجة عن الإخلال.',
     'قانون العقود'),
    (3, 2, 'القصد الجنائي في جرائم الاحتيال',
     'يقتضي إثبات الاحتيال توافر قصد التضليل بغية تحقيق كسب مالي، ولا يكفي مجرد الإهمال.',
     'القانون الجنائي');

INSERT INTO legislations (legislation_id, title, jurisdiction, enactment_date, status) VALUES
    (1, 'قانون السلوك المالي', 'اتحادي', '2010-06-01', 'ساري'),
    (2, 'قانون العقود التجارية', 'اتحادي', '1998-01-01', 'معدل'),
    (3, 'قانون الامتثال للتنظيم البيئي', 'اتحادي', '2015-09-20', 'ساري'),
    (4, 'قانون حماية المستهلك', 'محلي', '2005-11-11', 'ساري');

INSERT INTO case_legislation_citations (citation_id, case_id, legislation_id, article_section, citation_context) VALUES
    (1, 1, 2, 'المادة 8 (2)', 'استُند إليها لتحديد معيار الإخلال الجسيم بالعقد.'),
    (2, 2, 3, 'المادة 14', 'استُند إليها بشأن نطاق صلاحية الجهة التنظيمية على التصاريح البيئية.'),
    (3, 3, 1, 'المادة 12', 'استُند إليها كأساس لتهمة الاحتيال المسندة إلى المدعى عليها.'),
    (4, 4, 3, 'المادة 14', 'أُعيد الاستناد إليها في الاستئناف للطعن في تفسير نطاق الصلاحية التنظيمية.');
