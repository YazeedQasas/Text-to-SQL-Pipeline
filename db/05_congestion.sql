-- =============================================================================
-- Judicial congestion (الاختناق القضائي)
--
-- Classifies every unresolved case as within normal duration, under watch, or
-- congested, based on how long it has been pending against a per-category
-- threshold.
--
-- Run ONCE after 04_migrate_to_arabic.sql and 02_seed.sql:
--   File > Open SQL Script > db/05_congestion.sql, then Execute All (lightning bolt)
--
-- -----------------------------------------------------------------------------
-- Three design decisions worth knowing about, all easy to change:
--
-- 1. A VIEW, not a generated column. Congestion depends on today's date, and
--    MySQL forbids non-deterministic functions (CURDATE/NOW) inside a generated
--    column. So the status is computed at read time by `case_congestion`.
--
-- 2. The thresholds live in a TABLE, not inside the view. That keeps the policy
--    auditable and editable without a DDL change, and lets the assistant answer
--    "what is the threshold for inheritance cases?" by querying it. Each row
--    also carries the rationale from the guideline, so the assistant can explain
--    WHY a case is congested rather than just asserting it.
--
-- 3. The guidelines give RANGES ("3 إلى 6 أشهر", "سنتين إلى 3 سنوات"). Rather
--    than pick one number arbitrarily, the lower bound raises 'قيد المراقبة'
--    and the upper bound raises 'مختنقة'. Where the guideline gives a single
--    figure ("أكثر من سنة واحدة"), both bounds are that figure and there is no
--    watch band.
--
-- ASSUMPTION TO CHECK: "unresolved" here means status IN ('مفتوحة','قيد
-- الاستئناف'), not 'مفتوحة' alone. The criminal guideline says congestion is
-- measured "دون صدور حكم بات" — a case under appeal has no final judgment yet,
-- so it is still accumulating delay. If you want open-only, change the WHERE at
-- the bottom of the view to `c.status = 'مفتوحة'`.
-- =============================================================================

USE legal_db;

-- -----------------------------------------------------------------------------
-- 1. Case category — the axis congestion thresholds are defined on.
--
-- Separate from the existing `case_type`, which is free text describing the
-- subject matter ('مدني - عقود'). This column is the controlled classification
-- the policy keys off.
-- -----------------------------------------------------------------------------
ALTER TABLE cases
    ADD COLUMN case_category ENUM(
        'مخالفات وجنح بسيطة',
        'جنح كبرى ومستأنفة',
        'جنايات كبرى',
        'أحوال شخصية',
        'مدنية وتجارية ومالية',
        'مواريث وتركات',
        'إدارية'
    ) NULL AFTER case_type;

UPDATE cases SET case_category = 'مدنية وتجارية ومالية' WHERE case_number = 'CV-2023-0142';
UPDATE cases SET case_category = 'إدارية'                WHERE case_number = 'CV-2023-0198';
UPDATE cases SET case_category = 'جنح كبرى ومستأنفة'     WHERE case_number = 'CR-2022-0087';
UPDATE cases SET case_category = 'إدارية'                WHERE case_number = 'AP-2023-0021';
UPDATE cases SET case_category = 'مدنية وتجارية ومالية' WHERE case_number = 'CV-2024-0056';

-- -----------------------------------------------------------------------------
-- 2. The policy itself.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS congestion_rules (
    case_category ENUM(
        'مخالفات وجنح بسيطة',
        'جنح كبرى ومستأنفة',
        'جنايات كبرى',
        'أحوال شخصية',
        'مدنية وتجارية ومالية',
        'مواريث وتركات',
        'إدارية'
    ) NOT NULL PRIMARY KEY,
    watch_months     INT NOT NULL,
    congested_months INT NOT NULL,
    examples         VARCHAR(500) NOT NULL,
    rationale        TEXT NOT NULL
) ENGINE=InnoDB;

DELETE FROM congestion_rules;

INSERT INTO congestion_rules (case_category, watch_months, congested_months, examples, rationale) VALUES
    ('مخالفات وجنح بسيطة', 3, 6,
     'المشاجرات البسيطة، غرامات السير، الشيكات بدون رصيد',
     'يتطلب هذا النوع من القضايا إجراءات تحقيق مبسطة وجلسات مرافعة محدودة، وأي تمديد إضافي يعتبر مؤشراً على بطء غير مبرر.'),

    ('جنح كبرى ومستأنفة', 12, 12,
     'السرقة العادية، النصب والاحتيال، الإيذاء الذي يتطلب تقارير طبية',
     'تحتاج هذه القضايا إلى استماع لشهود وتقديم دفوع، لكن تجاوزها عتبة الاثني عشر شهراً ينقلها مباشرة إلى خانة القضايا المتراكمة والمختنقة.'),

    ('جنايات كبرى', 24, 36,
     'القتل العمد، الإرهاب، قضايا المخدرات الكبرى',
     'تأخذ الجنايات وقتاً أطول في جمع الأدلة الجنائية وإعداد تقارير الطب الشرعي والتحقيقات الموسعة من قبل النيابة العامة، ولكن استمرار المحاكمة سنوات طويلة دون حسم يمسّ بضمانات المحاكمة العادلة.'),

    ('أحوال شخصية', 6, 12,
     'الطلاق، الخلع، النفقة، حضانة الأطفال',
     'المماطلة فيها تؤثر مباشرة على حياة الأطفال واستقرارهم المادي، وغالباً ما تتأخر بسبب تهرب أحد الأطراف من التبليغ أو إخفاء الدخل الحقيقي لحساب النفقة.'),

    ('مدنية وتجارية ومالية', 12, 24,
     'النزاعات على العقود، المطالبات المالية، الشراكات التجارية، الشركات المفلسة',
     'تتطلب جولات طويلة من تبادل المذكرات، وتعتمد كلياً على تقارير الخبراء المحاسبين أو المهندسين لتحديد الحقوق، وهو ما يستهلك وقتاً طويلاً.'),

    ('مواريث وتركات', 24, 60,
     'تصفية أملاك المتوفى وتوزيعها على الورثة',
     'من أشد القضايا اختناقاً تاريخياً؛ بسبب كثرة عدد الورثة وتوزعهم في أماكن مختلفة، والنزاع على تقييم العقارات، أو ظهور وثائق ووصايا مشكوك في صحتها.'),

    ('إدارية', 18, 18,
     'إلغاء القرارات الإدارية، التعويض عن نزع الملكية، دعاوى الموظفين ضد الجهات الحكومية',
     'روتين المراسلات الحكومية، وبطء الجهات الإدارية في الرد على طلبات المحكمة لتوفير المستندات.');

-- -----------------------------------------------------------------------------
-- 3. Demo cases covering every category and every band.
--
-- Filing dates are relative to CURDATE() rather than fixed literals, so the
-- dataset keeps showing a healthy spread of statuses no matter when it is run.
-- Closed cases are included on purpose: they are old enough that they WOULD be
-- congested if they were still pending, which is what proves the view excludes
-- resolved cases rather than just happening to have none.
-- -----------------------------------------------------------------------------
INSERT INTO cases (case_id, case_number, title, court_id, case_type, case_category, filing_date, status) VALUES
    -- مخالفات وجنح بسيطة: watch 3, congested 6
    ( 6, 'MS-2026-0301', 'النيابة العامة ضد سامي الحديد',        1, 'مخالفة سير',        'مخالفات وجنح بسيطة',   DATE_SUB(CURDATE(), INTERVAL 2 MONTH),  'مفتوحة'),
    ( 7, 'MS-2026-0302', 'النيابة العامة ضد هالة الزعبي',        1, 'شيك بدون رصيد',     'مخالفات وجنح بسيطة',   DATE_SUB(CURDATE(), INTERVAL 4 MONTH),  'مفتوحة'),
    ( 8, 'MS-2025-0288', 'النيابة العامة ضد وليد السعدي',        1, 'مشاجرة بسيطة',      'مخالفات وجنح بسيطة',   DATE_SUB(CURDATE(), INTERVAL 9 MONTH),  'مفتوحة'),

    -- جنح كبرى ومستأنفة: watch 12, congested 12
    ( 9, 'JN-2025-0142', 'النيابة العامة ضد فادي العمري',        1, 'سرقة',              'جنح كبرى ومستأنفة',    DATE_SUB(CURDATE(), INTERVAL 8 MONTH),  'مفتوحة'),
    (10, 'JN-2024-0099', 'النيابة العامة ضد ريم القضاة',         1, 'نصب واحتيال',       'جنح كبرى ومستأنفة',    DATE_SUB(CURDATE(), INTERVAL 18 MONTH), 'مفتوحة'),

    -- جنايات كبرى: watch 24, congested 36
    (11, 'JY-2025-0017', 'النيابة العامة ضد ماهر الشوبكي',       3, 'جناية مخدرات',      'جنايات كبرى',          DATE_SUB(CURDATE(), INTERVAL 14 MONTH), 'مفتوحة'),
    (12, 'JY-2023-0008', 'النيابة العامة ضد عصام البدور',        3, 'قتل عمد',           'جنايات كبرى',          DATE_SUB(CURDATE(), INTERVAL 30 MONTH), 'مفتوحة'),
    (13, 'JY-2022-0003', 'النيابة العامة ضد نبيل الرواشدة',      3, 'جناية مخدرات كبرى', 'جنايات كبرى',          DATE_SUB(CURDATE(), INTERVAL 44 MONTH), 'مفتوحة'),

    -- أحوال شخصية: watch 6, congested 12
    (14, 'AS-2026-0455', 'رنا الخوالدة ضد سامر الخوالدة',        1, 'نفقة وحضانة',       'أحوال شخصية',          DATE_SUB(CURDATE(), INTERVAL 3 MONTH),  'مفتوحة'),
    (15, 'AS-2025-0410', 'منى الطويل ضد باسم الطويل',            1, 'طلاق وخلع',         'أحوال شخصية',          DATE_SUB(CURDATE(), INTERVAL 15 MONTH), 'مفتوحة'),

    -- مواريث وتركات: watch 24, congested 60
    (16, 'MW-2023-0021', 'ورثة المرحوم عبد الكريم الحاج',        1, 'تقسيم تركة',        'مواريث وتركات',        DATE_SUB(CURDATE(), INTERVAL 40 MONTH), 'مفتوحة'),
    (17, 'MW-2020-0007', 'ورثة المرحومة سعاد المومني',           1, 'تقسيم تركة متنازع عليها', 'مواريث وتركات',  DATE_SUB(CURDATE(), INTERVAL 70 MONTH), 'مفتوحة'),

    -- إدارية: watch 18, congested 18
    (18, 'ID-2025-0064', 'خالد النسور ضد وزارة الأشغال',         2, 'إلغاء قرار إداري',  'إدارية',               DATE_SUB(CURDATE(), INTERVAL 10 MONTH), 'مفتوحة'),
    (19, 'ID-2024-0031', 'شركة البناء الحديث ضد أمانة العاصمة',  2, 'تعويض نزع ملكية',   'إدارية',               DATE_SUB(CURDATE(), INTERVAL 26 MONTH), 'مفتوحة'),

    -- Resolved cases, old enough to be congested if they were still pending.
    (20, 'MD-2023-0512', 'شركة الأفق ضد مؤسسة النماء',           1, 'نزاع عقد توريد',    'مدنية وتجارية ومالية', DATE_SUB(CURDATE(), INTERVAL 30 MONTH), 'مغلقة'),
    (21, 'AS-2024-0377', 'ليث الشمري ضد دعاء الشمري',            1, 'نفقة',              'أحوال شخصية',          DATE_SUB(CURDATE(), INTERVAL 20 MONTH), 'مغلقة'),
    (22, 'MS-2024-0250', 'النيابة العامة ضد أنس الخطيب',         1, 'مخالفة سير',        'مخالفات وجنح بسيطة',   DATE_SUB(CURDATE(), INTERVAL 22 MONTH), 'مشطوبة');

-- -----------------------------------------------------------------------------
-- 3b. Relational data for the new cases.
--
-- Without this the congestion view works but nothing else does — "من يمثّل
-- المدعى عليه في القضية المختنقة؟" or "أي قاضٍ ينظر أطول القضايا؟" would come
-- back empty for every case added above. Each new case gets its parties, their
-- counsel, and its hearing history.
-- -----------------------------------------------------------------------------

-- Two more advocates, so counsel is spread across more than the original four.
INSERT INTO lawyers (lawyer_id, full_name, bar_number, firm_name) VALUES
    (5, 'عمر الزيود',  'BAR-11340', 'مكتب الزيود للمحاماة'),
    (6, 'هدى المصري',  'BAR-11567', NULL);

INSERT INTO parties (party_id, name, party_type, contact_info) VALUES
    ( 6, 'النيابة العامة',            'جهة اعتبارية', 'prosecution@judiciary.example.gov'),
    ( 7, 'سامي الحديد',               'شخص طبيعي',   'sami.hadid@example.com'),
    ( 8, 'هالة الزعبي',               'شخص طبيعي',   'hala.zoubi@example.com'),
    ( 9, 'وليد السعدي',               'شخص طبيعي',   'walid.saadi@example.com'),
    (10, 'فادي العمري',               'شخص طبيعي',   'fadi.amri@example.com'),
    (11, 'ريم القضاة',                'شخص طبيعي',   'reem.qudah@example.com'),
    (12, 'ماهر الشوبكي',              'شخص طبيعي',   'maher.shobaki@example.com'),
    (13, 'عصام البدور',               'شخص طبيعي',   'issam.badour@example.com'),
    (14, 'نبيل الرواشدة',             'شخص طبيعي',   'nabil.rawashdeh@example.com'),
    (15, 'أنس الخطيب',                'شخص طبيعي',   'anas.khatib@example.com'),
    (16, 'رنا الخوالدة',              'شخص طبيعي',   'rana.khawaldeh@example.com'),
    (17, 'سامر الخوالدة',             'شخص طبيعي',   'samer.khawaldeh@example.com'),
    (18, 'منى الطويل',                'شخص طبيعي',   'mona.tawil@example.com'),
    (19, 'باسم الطويل',               'شخص طبيعي',   'basem.tawil@example.com'),
    (20, 'ليث الشمري',                'شخص طبيعي',   'laith.shammari@example.com'),
    (21, 'دعاء الشمري',               'شخص طبيعي',   'duaa.shammari@example.com'),
    (22, 'خالد النسور',               'شخص طبيعي',   'khaled.nsour@example.com'),
    (23, 'وزارة الأشغال العامة',      'جهة اعتبارية', 'info@mpw.example.gov'),
    (24, 'شركة البناء الحديث',        'جهة اعتبارية', 'legal@modernbuild.example.com'),
    (25, 'أمانة عمّان الكبرى',         'جهة اعتبارية', 'legal@gam.example.gov'),
    (26, 'فايز الحاج',                'شخص طبيعي',   'fayez.hajj@example.com'),
    (27, 'سلمى الحاج',                'شخص طبيعي',   'salma.hajj@example.com'),
    (28, 'هيثم المومني',              'شخص طبيعي',   'haitham.momani@example.com'),
    (29, 'لينا المومني',              'شخص طبيعي',   'lina.momani@example.com'),
    (30, 'شركة الأفق للتجارة',        'جهة اعتبارية', 'legal@ufuq.example.com'),
    (31, 'مؤسسة النماء',              'جهة اعتبارية', 'contact@namaa.example.com');

INSERT INTO case_parties (case_party_id, case_id, party_id, role, lawyer_id) VALUES
    -- 6 MS-2026-0301
    (10,  6,  6, 'مدعي',       NULL),
    (11,  6,  7, 'مدعى عليه',  5),
    -- 7 MS-2026-0302
    (12,  7,  6, 'مدعي',       NULL),
    (13,  7,  8, 'مدعى عليه',  6),
    -- 8 MS-2025-0288
    (14,  8,  6, 'مدعي',       NULL),
    (15,  8,  9, 'مدعى عليه',  1),
    -- 9 JN-2025-0142
    (16,  9,  6, 'مدعي',       NULL),
    (17,  9, 10, 'مدعى عليه',  2),
    -- 10 JN-2024-0099
    (18, 10,  6, 'مدعي',       NULL),
    (19, 10, 11, 'مدعى عليه',  5),
    -- 11 JY-2025-0017
    (20, 11,  6, 'مدعي',       NULL),
    (21, 11, 12, 'مدعى عليه',  3),
    -- 12 JY-2023-0008
    (22, 12,  6, 'مدعي',       NULL),
    (23, 12, 13, 'مدعى عليه',  6),
    -- 13 JY-2022-0003
    (24, 13,  6, 'مدعي',       NULL),
    (25, 13, 14, 'مدعى عليه',  2),
    -- 14 AS-2026-0455
    (26, 14, 16, 'مدعي',       6),
    (27, 14, 17, 'مدعى عليه',  5),
    -- 15 AS-2025-0410
    (28, 15, 18, 'مدعي',       1),
    (29, 15, 19, 'مدعى عليه',  4),
    -- 16 MW-2023-0021 — inheritance, several heirs
    (30, 16, 26, 'مدعي',       2),
    (31, 16, 27, 'مدعى عليه',  5),
    (32, 16, 22, 'طرف ثالث',   NULL),
    -- 17 MW-2020-0007
    (33, 17, 28, 'مدعي',       6),
    (34, 17, 29, 'مدعى عليه',  1),
    -- 18 ID-2025-0064
    (35, 18, 22, 'مدعي',       5),
    (36, 18, 23, 'مدعى عليه',  3),
    -- 19 ID-2024-0031
    (37, 19, 24, 'مدعي',       2),
    (38, 19, 25, 'مدعى عليه',  6),
    -- 20 MD-2023-0512 (مغلقة)
    (39, 20, 30, 'مدعي',       1),
    (40, 20, 31, 'مدعى عليه',  4),
    -- 21 AS-2024-0377 (مغلقة)
    (41, 21, 20, 'مدعي',       6),
    (42, 21, 21, 'مدعى عليه',  5),
    -- 22 MS-2024-0250 (مشطوبة)
    (43, 22,  6, 'مدعي',       NULL),
    (44, 22, 15, 'مدعى عليه',  3);

-- Hearing history. Dates are anchored to each case's filing date so a case that
-- has been pending 44 months genuinely shows a long, dragging history rather
-- than hearings clustered at the start.
INSERT INTO hearings (hearing_id, case_id, judge_id, hearing_date, hearing_type, outcome_notes) VALUES
    ( 8,  6, 2, DATE_SUB(CURDATE(), INTERVAL 1 MONTH),  'جلسة تمهيدية',     'تم تبليغ المدعى عليه وحُدّدت جلسة المرافعة.'),
    ( 9,  7, 2, DATE_SUB(CURDATE(), INTERVAL 3 MONTH),  'جلسة تمهيدية',     'طُلب من البنك تقديم كشف الحساب.'),
    (10,  7, 2, DATE_SUB(CURDATE(), INTERVAL 1 MONTH),  'محاكمة',           'أُجّلت الجلسة لعدم حضور ممثل البنك.'),
    (11,  8, 1, DATE_SUB(CURDATE(), INTERVAL 7 MONTH),  'جلسة اتهام',       'أنكر المدعى عليه التهمة.'),
    (12,  8, 1, DATE_SUB(CURDATE(), INTERVAL 4 MONTH),  'محاكمة',           'أُجّلت لتعذّر حضور الشهود.'),
    (13,  8, 1, DATE_SUB(CURDATE(), INTERVAL 1 MONTH),  'محاكمة',           'أُجّلت مجدداً لعدم اكتمال البيّنة.'),
    (14,  9, 2, DATE_SUB(CURDATE(), INTERVAL 6 MONTH),  'جلسة اتهام',       'أنكر المدعى عليه التهمة المسندة إليه.'),
    (15,  9, 2, DATE_SUB(CURDATE(), INTERVAL 2 MONTH),  'محاكمة',           'استُمع إلى شاهدَي الإثبات.'),
    (16, 10, 1, DATE_SUB(CURDATE(), INTERVAL 16 MONTH), 'جلسة اتهام',       'أنكرت المدعى عليها التهمة.'),
    (17, 10, 1, DATE_SUB(CURDATE(), INTERVAL 9 MONTH),  'محاكمة',           'قُدّم تقرير الخبير المحاسبي.'),
    (18, 10, 1, DATE_SUB(CURDATE(), INTERVAL 3 MONTH),  'محاكمة',           'أُجّلت لطلب الدفاع مهلة لتقديم بيّنة الدفع.'),
    (19, 11, 4, DATE_SUB(CURDATE(), INTERVAL 12 MONTH), 'جلسة اتهام',       'أُحيلت القضية إلى محكمة الجنايات.'),
    (20, 11, 4, DATE_SUB(CURDATE(), INTERVAL 5 MONTH),  'محاكمة',           'استُمع إلى تقرير المختبر الجنائي.'),
    (21, 12, 4, DATE_SUB(CURDATE(), INTERVAL 28 MONTH), 'جلسة اتهام',       'أُحيلت القضية إلى محكمة الجنايات.'),
    (22, 12, 4, DATE_SUB(CURDATE(), INTERVAL 18 MONTH), 'محاكمة',           'أُجّلت بانتظار تقرير الطب الشرعي.'),
    (23, 12, 4, DATE_SUB(CURDATE(), INTERVAL 6 MONTH),  'محاكمة',           'ما زالت البيّنة قيد الاستكمال.'),
    (24, 13, 4, DATE_SUB(CURDATE(), INTERVAL 42 MONTH), 'جلسة اتهام',       'أنكر المدعى عليه التهم المسندة إليه.'),
    (25, 13, 4, DATE_SUB(CURDATE(), INTERVAL 30 MONTH), 'محاكمة',           'أُجّلت لاستكمال التحقيقات الموسعة.'),
    (26, 13, 4, DATE_SUB(CURDATE(), INTERVAL 14 MONTH), 'محاكمة',           'أُجّلت لتعذّر إحضار أحد المتهمين.'),
    (27, 13, 4, DATE_SUB(CURDATE(), INTERVAL 2 MONTH),  'محاكمة',           'ما زالت القضية دون حسم.'),
    (28, 14, 1, DATE_SUB(CURDATE(), INTERVAL 2 MONTH),  'جلسة تمهيدية',     'أُحيل الطرفان إلى مكتب الإصلاح الأسري.'),
    (29, 15, 1, DATE_SUB(CURDATE(), INTERVAL 13 MONTH), 'جلسة تمهيدية',     'تعذّر تبليغ المدعى عليه.'),
    (30, 15, 1, DATE_SUB(CURDATE(), INTERVAL 7 MONTH),  'محاكمة',           'طُلب كشف عن دخل المدعى عليه لتقدير النفقة.'),
    (31, 15, 1, DATE_SUB(CURDATE(), INTERVAL 2 MONTH),  'محاكمة',           'أُجّلت لعدم ورود كشف الدخل.'),
    (32, 16, 1, DATE_SUB(CURDATE(), INTERVAL 36 MONTH), 'جلسة تمهيدية',     'حُصر الورثة وطُلب حصر إرث.'),
    (33, 16, 1, DATE_SUB(CURDATE(), INTERVAL 20 MONTH), 'محاكمة',           'نزاع على تقييم العقارات، أُحيلت إلى خبير.'),
    (34, 16, 1, DATE_SUB(CURDATE(), INTERVAL 5 MONTH),  'محاكمة',           'اعتُرض على تقرير الخبير وطُلبت خبرة ثانية.'),
    (35, 17, 1, DATE_SUB(CURDATE(), INTERVAL 66 MONTH), 'جلسة تمهيدية',     'طُلب حصر إرث وتحديد الورثة.'),
    (36, 17, 1, DATE_SUB(CURDATE(), INTERVAL 40 MONTH), 'محاكمة',           'ظهرت وصية مشكوك في صحتها وأُحيلت للخبرة.'),
    (37, 17, 1, DATE_SUB(CURDATE(), INTERVAL 15 MONTH), 'محاكمة',           'أُجّلت لتعذّر حضور أحد الورثة المقيم خارج البلاد.'),
    (38, 18, 3, DATE_SUB(CURDATE(), INTERVAL 8 MONTH),  'مراجعة استئنافية', 'طُلب من الوزارة تقديم ملف القرار الإداري.'),
    (39, 18, 3, DATE_SUB(CURDATE(), INTERVAL 2 MONTH),  'مراجعة استئنافية', 'لم ترد الوزارة على طلب المحكمة.'),
    (40, 19, 3, DATE_SUB(CURDATE(), INTERVAL 24 MONTH), 'مراجعة استئنافية', 'طُلبت مستندات نزع الملكية من الأمانة.'),
    (41, 19, 3, DATE_SUB(CURDATE(), INTERVAL 12 MONTH), 'مراجعة استئنافية', 'أُحيل تقدير التعويض إلى لجنة خبراء.'),
    (42, 19, 3, DATE_SUB(CURDATE(), INTERVAL 3 MONTH),  'مراجعة استئنافية', 'ما زال تقرير اللجنة غير مكتمل.'),
    (43, 20, 2, DATE_SUB(CURDATE(), INTERVAL 26 MONTH), 'جلسة تمهيدية',     'حُدّد جدول تبادل المذكرات.'),
    (44, 20, 2, DATE_SUB(CURDATE(), INTERVAL 14 MONTH), 'محاكمة',           'قُدّم تقرير الخبير المحاسبي.'),
    (45, 21, 1, DATE_SUB(CURDATE(), INTERVAL 18 MONTH), 'جلسة تمهيدية',     'اتفق الطرفان على مبدأ التسوية.'),
    (46, 22, 2, DATE_SUB(CURDATE(), INTERVAL 20 MONTH), 'جلسة تمهيدية',     'تخلّف المدعى عليه عن الحضور مرتين.');

-- Judgements for the two cases that actually concluded. The dismissed case
-- (MS-2024-0250, مشطوبة) deliberately has none — it was struck out, not ruled on.
INSERT INTO judgements (judgement_id, case_id, judge_id, decision_date, verdict, summary, full_text_url) VALUES
    (3, 20, 2, DATE_SUB(CURDATE(), INTERVAL 8 MONTH), 'الحكم لصالح المدعي',
     'وجدت المحكمة أن مؤسسة النماء أخلّت بشروط التوريد المتفق عليها مع شركة الأفق للتجارة، وقضت بإلزامها بالتعويض وفق تقرير الخبير المحاسبي.',
     'https://records.example.gov/judgements/3'),
    (4, 21, 1, DATE_SUB(CURDATE(), INTERVAL 15 MONTH), 'الحكم لصالح المدعي',
     'صادقت المحكمة على التسوية الودية بين الطرفين وحدّدت مقدار النفقة الشهرية ووسيلة تسديدها.',
     'https://records.example.gov/judgements/4');

INSERT INTO principles (principle_id, judgement_id, title, description, area_of_law) VALUES
    (4, 3, 'حجية تقرير الخبير المحاسبي',
     'يُعتد بتقرير الخبير المحاسبي في تحديد مقدار التعويض ما لم يقدّم الطرف المعترض بيّنة فنية مضادة تدحض أسسه.',
     'قانون العقود');

-- -----------------------------------------------------------------------------
-- 4. The view.
--
-- LEFT JOIN, not INNER: an unclassified case must surface as 'غير مصنفة'
-- rather than silently vanishing from congestion reporting.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW case_congestion AS
SELECT
    c.case_id,
    c.case_number,
    c.title,
    c.case_category,
    c.status,
    c.filing_date,
    c.court_id,
    TIMESTAMPDIFF(MONTH, c.filing_date, CURDATE())                       AS months_pending,
    r.watch_months,
    r.congested_months,
    CASE
        WHEN r.case_category IS NULL THEN 'غير مصنفة'
        WHEN TIMESTAMPDIFF(MONTH, c.filing_date, CURDATE()) > r.congested_months THEN 'مختنقة'
        WHEN TIMESTAMPDIFF(MONTH, c.filing_date, CURDATE()) > r.watch_months     THEN 'قيد المراقبة'
        ELSE 'ضمن المدة الطبيعية'
    END                                                                   AS congestion_status,
    GREATEST(TIMESTAMPDIFF(MONTH, c.filing_date, CURDATE()) - r.congested_months, 0)
                                                                          AS months_over_threshold,
    r.rationale
FROM cases c
LEFT JOIN congestion_rules r ON c.case_category = r.case_category
WHERE c.status IN ('مفتوحة', 'قيد الاستئناف');

-- -----------------------------------------------------------------------------
-- Check it worked.
--
-- 1. Distribution across the three bands:
--   SELECT congestion_status, COUNT(*) AS n
--     FROM case_congestion GROUP BY congestion_status;
--
-- 2. Worst offenders first:
--   SELECT case_number, case_category, months_pending, congested_months,
--          congestion_status, months_over_threshold
--     FROM case_congestion ORDER BY months_over_threshold DESC, months_pending DESC;
--
-- 3. Resolved cases must NOT appear, even though they are old enough to qualify:
--   SELECT case_number FROM case_congestion WHERE case_number IN
--     ('MD-2023-0512','AS-2024-0377','MS-2024-0250');   -- expect zero rows
--
-- 4. The relational data is wired up — counsel on a congested case:
--   SELECT cc.case_number, cc.congestion_status, p.name, cp.role, l.full_name
--     FROM case_congestion cc
--     JOIN case_parties cp ON cp.case_id = cc.case_id
--     JOIN parties p       ON p.party_id = cp.party_id
--     LEFT JOIN lawyers l  ON l.lawyer_id = cp.lawyer_id
--    WHERE cc.congestion_status = 'مختنقة';
--
-- 5. Which judge is sitting on the most congested cases:
--   SELECT j.full_name, COUNT(DISTINCT cc.case_id) AS n
--     FROM case_congestion cc
--     JOIN hearings h ON h.case_id = cc.case_id
--     JOIN judges  j  ON j.judge_id = h.judge_id
--    WHERE cc.congestion_status = 'مختنقة'
--    GROUP BY j.full_name ORDER BY n DESC;
--
-- Do NOT re-run ingest.py yet — tell me once this has run and I will read the
-- real column types off the view before writing its schema document, rather
-- than guessing what MySQL reports for TIMESTAMPDIFF and CASE expressions.
-- -----------------------------------------------------------------------------
