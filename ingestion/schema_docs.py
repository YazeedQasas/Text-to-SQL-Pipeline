"""
Schema document definitions for the legal_db demo database (Arabic).

This module is the single source of truth for what gets embedded and stored
in Qdrant for retrieval-augmented SQL generation. It mirrors db/01_schema.sql
exactly — if you change the SQL schema, update the corresponding TableDoc here.

Language policy
---------------
- Descriptions (table and column) are written in ARABIC, because they are what
  the retriever matches an Arabic user question against.
- Identifiers — table names, column names, foreign key targets — stay in
  ENGLISH, and are referenced verbatim inside the Arabic prose. The user never
  sees them, the LLM writes measurably better SQL against them, and it avoids
  having to backtick-quote every identifier in generated queries.
- Structural labels emitted by `build_table_doc_text` ("Table:", "Columns:",
  "Primary key:") also stay English so this file and the backend's prompt
  builder (`app/services/retrieval.py::format_tables_for_prompt`) render the
  same scaffolding.
- ENUM values are still stored in English in MySQL, so each ENUM column's
  description glosses them in Arabic (e.g. "'Open' (مفتوحة)"). That mapping is
  what lets the model turn "القضايا المفتوحة" into `status = 'Open'`. When the
  underlying data is migrated to Arabic, update these glosses to match.

Design notes
------------
- Granularity: every doc built here is a TABLE-level document (DOC_LEVEL_TABLE).
  Each ColumnDoc already carries its own name/type/description, so a future
  column-level retrieval mode can slice these same structures into individual
  per-column documents (DOC_LEVEL_COLUMN) without redefining the metadata —
  see ingest.py's `build_column_documents` stub.
- Description quality matters more than any other field here: the retriever
  matches a natural-language question against `description` (and to a lesser
  extent column descriptions), so descriptions should name the real-world
  entities and relationships a user would ask about, not just restate the
  column list.
"""

from dataclasses import dataclass, field

DOC_LEVEL_TABLE = "table"
DOC_LEVEL_COLUMN = "column"  # reserved for future column-level ingestion


@dataclass
class ColumnDoc:
    name: str
    type: str
    nullable: bool
    description: str


@dataclass
class ForeignKeyDoc:
    column: str
    references_table: str
    references_column: str


@dataclass
class TableDoc:
    name: str
    description: str
    columns: list[ColumnDoc]
    primary_key: str
    foreign_keys: list[ForeignKeyDoc] = field(default_factory=list)


TABLES: list[TableDoc] = [
    TableDoc(
        name="courts",
        # Kept deliberately narrow: this table answers questions about the
        # institution itself (name, level, jurisdiction, location). Earlier
        # wording also enumerated cases and judges, which made it outrank
        # `judges` on judge questions — in Arabic قضية / قاضٍ / قضائية share the
        # ق-ض-ي root, so naming them here pulls this table into any question
        # about either.
        description=(
            "المحاكم كمؤسسات قضائية: بياناتها التعريفية مثل اسمها الرسمي، ودرجتها في "
            "التسلسل القضائي (ابتدائية أو استئناف أو عليا)، ونطاق اختصاصها (اتحادي أو "
            "ولائي)، والمدينة أو المنطقة التي تقع فيها. استخدم هذا الجدول عندما يكون "
            "السؤال عن المحكمة نفسها — اسمها أو درجتها أو اختصاصها أو مقرها — أو للحصول "
            "على اسم المحكمة بعد الوصول إليها من جدول آخر عبر court_id."
        ),
        primary_key="court_id",
        columns=[
            ColumnDoc("court_id", "INT", False, "معرّف فريد للمحكمة."),
            ColumnDoc("name", "VARCHAR(255)", False, "الاسم الرسمي للمحكمة، مثل 'المحكمة العليا'."),
            ColumnDoc("level", "ENUM('ابتدائية','استئناف','عليا')", False, "درجة المحكمة في التسلسل القضائي. القيم المخزَّنة حصرًا: 'ابتدائية'، 'استئناف'، 'عليا'."),
            ColumnDoc("jurisdiction", "VARCHAR(255)", False, "الاختصاص القضائي للمحكمة. القيم المخزَّنة: 'اتحادي'، 'محلي'."),
            ColumnDoc("location", "VARCHAR(255)", False, "المدينة أو المنطقة التي تقع فيها المحكمة، مثل 'عمّان'، 'إربد'."),
        ],
    ),
    TableDoc(
        name="judges",
        description=(
            "القضاة، ويُعرَّفون بالاسم الكامل (full_name)، الذين ينظرون في الجلسات ويصدرون "
            "الأحكام. كل قاضٍ مرتبط بمحكمة واحدة يتبع لها. هذا هو الجدول الذي يُرجَع إليه "
            "كلما ورد اسم قاضٍ محدد في السؤال (مثل 'القاضي فلان') — يُستخرج judge_id من "
            "full_name هنا، ثم يُربط بجدول hearings أو judgements عبر judge_id لمعرفة "
            "القضايا التي نظر فيها أو أصدر أحكامًا بشأنها. يتضمن أيضًا اللقب القضائي "
            "وتاريخ التعيين والمحكمة التابع لها."
        ),
        primary_key="judge_id",
        foreign_keys=[ForeignKeyDoc("court_id", "courts", "court_id")],
        columns=[
            ColumnDoc("judge_id", "INT", False, "معرّف فريد للقاضي."),
            ColumnDoc("full_name", "VARCHAR(255)", False, "الاسم الكامل للقاضي كما هو مخزَّن. يُستخدم للعرض فقط؛ للبحث عن اسم استعمل full_name_norm."),
            ColumnDoc("title", "VARCHAR(100)", False, "اللقب القضائي، مثل 'رئيسة المحكمة'، 'قاضٍ مشارك'، 'قاضية استئناف'، 'رئيس القضاة'."),
            ColumnDoc("court_id", "INT", False, "المحكمة التي يتبع لها القاضي. مفتاح خارجي إلى courts.court_id."),
            ColumnDoc("appointed_date", "DATE", True, "تاريخ تعيين القاضي في منصبه القضائي."),
            ColumnDoc("full_name_norm", "VARCHAR(255)", True, "نسخة مُوحَّدة إملائيًا من full_name، يولّدها النظام تلقائيًا: الهمزات (أ إ آ) تُردّ إلى ا، والتاء المربوطة ة إلى ه، والألف المقصورة ى إلى ي، وتُحذف التشكيلات والتطويل. استخدم هذا العمود دائمًا عند البحث عن اسم قاضٍ، مع كتابة المصطلح المطلوب بالصيغة المُوحَّدة نفسها."),
        ],
    ),
    TableDoc(
        name="lawyers",
        description=(
            "المحامون (وكلاء الأطراف) الذين يمثلون أطراف القضية. استخدم هذا الجدول للإجابة "
            "عن الأسئلة المتعلقة بهوية المحامي، أو رقم قيده في نقابة المحامين، أو المكتب "
            "القانوني الذي ينتمي إليه. أما تحديد أي محامٍ مثّل أي طرف في أي قضية فمسجَّل في "
            "جدول case_parties وليس هنا — يُربط عبر case_parties.lawyer_id."
        ),
        primary_key="lawyer_id",
        columns=[
            ColumnDoc("lawyer_id", "INT", False, "معرّف فريد للمحامي."),
            ColumnDoc("full_name", "VARCHAR(255)", False, "الاسم الكامل للمحامي كما هو مخزَّن. يُستخدم للعرض فقط؛ للبحث عن اسم استعمل full_name_norm."),
            ColumnDoc("bar_number", "VARCHAR(50)", False, "رقم القيد الفريد في نقابة المحامين، ويُكتب بالحروف اللاتينية مثل 'BAR-10023'."),
            ColumnDoc("firm_name", "VARCHAR(255)", True, "اسم المكتب القانوني الذي يعمل به المحامي، إن وُجد (فارغ للمحامين المستقلين)."),
            ColumnDoc("full_name_norm", "VARCHAR(255)", True, "نسخة مُوحَّدة إملائيًا من full_name يولّدها النظام تلقائيًا. استخدم هذا العمود دائمًا عند البحث عن اسم محامٍ."),
        ],
    ),
    TableDoc(
        name="parties",
        description=(
            "الأشخاص أو الجهات المشاركون في القضية — المدّعون والمدّعى عليهم والمستأنفون "
            "والمستأنف ضدهم وغيرهم. هذا الجدول يحفظ اسم الشخص أو الجهة وبيانات التواصل "
            "فقط؛ أما صفته في القضية والقضية التي شارك فيها فمسجَّلة في case_parties وليس "
            "هنا. استخدم هذا الجدول لمعرفة هوية شخص أو جهة ورد اسمها في السؤال، ثم اربطه "
            "عبر case_parties للوصول إلى قضاياه."
        ),
        primary_key="party_id",
        columns=[
            ColumnDoc("party_id", "INT", False, "معرّف فريد للطرف."),
            ColumnDoc("name", "VARCHAR(255)", False, "الاسم الكامل للشخص أو الجهة كما هو مخزَّن. يُستخدم للعرض فقط؛ للبحث عن اسم استعمل name_norm."),
            ColumnDoc("party_type", "ENUM('شخص طبيعي','جهة اعتبارية')", False, "نوع الطرف. القيم المخزَّنة حصرًا: 'شخص طبيعي'، 'جهة اعتبارية'."),
            ColumnDoc("contact_info", "VARCHAR(255)", True, "البريد الإلكتروني أو وسيلة تواصل أخرى للطرف."),
            ColumnDoc("name_norm", "VARCHAR(255)", True, "نسخة مُوحَّدة إملائيًا من name يولّدها النظام تلقائيًا. استخدم هذا العمود دائمًا عند البحث عن اسم شخص أو جهة."),
        ],
    ),
    TableDoc(
        name="cases",
        description=(
            "السجل الرئيسي للقضية (دعوى مدنية أو جنائية) المرفوعة أمام محكمة. وهو عادةً "
            "نقطة البداية للأسئلة التي تدور حول 'قضية' — رقمها وعنوانها ونوعها (مدنية، "
            "جنائية، استئناف، وغيرها) وتاريخ رفعها وحالتها الراهنة. وكل ما عداه يدور حول "
            "القضية: أطرافها ومحاموهم (case_parties)، وجلساتها (hearings)، وحكمها النهائي "
            "(judgements)، والتشريعات التي استندت إليها (case_legislation_citations)."
        ),
        primary_key="case_id",
        foreign_keys=[ForeignKeyDoc("court_id", "courts", "court_id")],
        columns=[
            ColumnDoc("case_id", "INT", False, "معرّف فريد للقضية."),
            ColumnDoc("case_number", "VARCHAR(50)", False, "رقم القضية الرسمي في سجل المحكمة، مثل 'CV-2023-0142'."),
            ColumnDoc("title", "VARCHAR(500)", False, "عنوان القضية، وعادةً بصيغة 'فلان ضد فلان'. يُستخدم للعرض؛ للبحث في العنوان استعمل title_norm."),
            ColumnDoc("court_id", "INT", False, "المحكمة التي رُفعت أمامها القضية. مفتاح خارجي إلى courts.court_id."),
            ColumnDoc("case_type", "VARCHAR(100)", False, "تصنيف القضية. القيم المخزَّنة: 'مدني - عقود'، 'مدني - تنظيمي'، 'مدني - استئناف تنظيمي'، 'مدني - دعوى متقابلة'، 'جنائي - احتيال'."),
            ColumnDoc("filing_date", "DATE", False, "تاريخ رفع القضية أمام المحكمة."),
            ColumnDoc("status", "ENUM('مفتوحة','مغلقة','قيد الاستئناف','مشطوبة')", False, "الحالة الراهنة للقضية. القيم المخزَّنة حصرًا: 'مفتوحة' (منظورة)، 'مغلقة' (منتهية)، 'قيد الاستئناف'، 'مشطوبة' (مرفوضة)."),
            ColumnDoc("title_norm", "VARCHAR(500)", True, "نسخة مُوحَّدة إملائيًا من title يولّدها النظام تلقائيًا. استخدم هذا العمود عند البحث عن قضية باسم أحد أطرافها."),
        ],
    ),
    TableDoc(
        name="case_parties",
        description=(
            "جدول ربط يصل القضايا بالأطراف المشاركين فيها، ويسجّل صفة كل طرف (مدّعٍ، مدّعى "
            "عليه، مستأنف، مستأنف ضده، طرف ثالث)، وكذلك المحامي الذي مثّله في تلك القضية "
            "إن وُجد. استخدم هذا الجدول للإجابة عن 'من هم المدّعون أو المدّعى عليهم في "
            "القضية الفلانية'، أو 'ما القضايا التي شارك فيها فلان'، أو 'أي محامٍ مثّل فلانًا "
            "في قضية معينة'. وهو محور الربط بين cases وparties وlawyers."
        ),
        primary_key="case_party_id",
        foreign_keys=[
            ForeignKeyDoc("case_id", "cases", "case_id"),
            ForeignKeyDoc("party_id", "parties", "party_id"),
            ForeignKeyDoc("lawyer_id", "lawyers", "lawyer_id"),
        ],
        columns=[
            ColumnDoc("case_party_id", "INT", False, "معرّف فريد لهذا الربط بين القضية والطرف."),
            ColumnDoc("case_id", "INT", False, "القضية التي شارك فيها الطرف. مفتاح خارجي إلى cases.case_id."),
            ColumnDoc("party_id", "INT", False, "الطرف المشارك في القضية. مفتاح خارجي إلى parties.party_id."),
            ColumnDoc("role", "ENUM('مدعي','مدعى عليه','مستأنف','مستأنف ضده','طرف ثالث')", False, "صفة الطرف في هذه القضية تحديدًا. القيم المخزَّنة حصرًا: 'مدعي'، 'مدعى عليه'، 'مستأنف'، 'مستأنف ضده'، 'طرف ثالث'."),
            ColumnDoc("lawyer_id", "INT", True, "المحامي الذي يمثل هذا الطرف في هذه القضية، إن سُجِّل. مفتاح خارجي إلى lawyers.lawyer_id."),
        ],
    ),
    TableDoc(
        name="hearings",
        description=(
            "جلسات المحكمة المنعقدة لقضية ما (جلسات تمهيدية، محاكمات، جلسات اتهام، "
            "مراجعات استئنافية وغيرها)، ويرأس كلًّا منها قاضٍ واحد. استخدم هذا الجدول "
            "للإجابة عن متى نُظرت القضية، وما نوع الجلسة، وما جرى فيها أو ما تقرر "
            "(outcome_notes)، وأي قاضٍ ترأسها. وعادةً ما يكون للقضية عدة جلسات على مدى "
            "الوقت، أما حكمها النهائي فمسجَّل على حدة في judgements."
        ),
        primary_key="hearing_id",
        foreign_keys=[
            ForeignKeyDoc("case_id", "cases", "case_id"),
            ForeignKeyDoc("judge_id", "judges", "judge_id"),
        ],
        columns=[
            ColumnDoc("hearing_id", "INT", False, "معرّف فريد للجلسة."),
            ColumnDoc("case_id", "INT", False, "القضية التي تتبع لها هذه الجلسة. مفتاح خارجي إلى cases.case_id."),
            ColumnDoc("judge_id", "INT", False, "القاضي الذي ترأس الجلسة. مفتاح خارجي إلى judges.judge_id."),
            ColumnDoc("hearing_date", "DATETIME", False, "تاريخ ووقت انعقاد الجلسة."),
            ColumnDoc("hearing_type", "VARCHAR(100)", False, "نوع الجلسة. القيم المخزَّنة: 'جلسة تمهيدية'، 'محاكمة'، 'جلسة اتهام'، 'مراجعة استئنافية'."),
            ColumnDoc("outcome_notes", "TEXT", True, "ملاحظات نصية حرة عمّا جرى أو ما تقرر في الجلسة."),
        ],
    ),
    TableDoc(
        name="judgements",
        description=(
            "الأحكام النهائية الصادرة في القضايا عن القضاة — وهي النتيجة الرسمية "
            "والفاصلة للقضية. استخدم هذا الجدول للإجابة عن 'ما الحكم الصادر في القضية "
            "الفلانية'، أو 'من كسب الدعوى'، أو 'بماذا قضت المحكمة ولماذا' (summary). أما "
            "المبادئ القانونية التي أرساها الحكم فمسجَّلة في جدول principles ويُربط بها عبر "
            "judgement_id. وليست كل قضية لها حكم بعد (القضايا المفتوحة أو قيد الاستئناف قد "
            "لا يكون لها حكم)."
        ),
        primary_key="judgement_id",
        foreign_keys=[
            ForeignKeyDoc("case_id", "cases", "case_id"),
            ForeignKeyDoc("judge_id", "judges", "judge_id"),
        ],
        columns=[
            ColumnDoc("judgement_id", "INT", False, "معرّف فريد للحكم."),
            ColumnDoc("case_id", "INT", False, "القضية التي صدر فيها هذا الحكم. مفتاح خارجي إلى cases.case_id."),
            ColumnDoc("judge_id", "INT", False, "القاضي الذي أصدر الحكم. مفتاح خارجي إلى judges.judge_id."),
            ColumnDoc("decision_date", "DATE", False, "تاريخ صدور الحكم."),
            ColumnDoc("verdict", "VARCHAR(255)", False, "منطوق الحكم المختصر. القيم المخزَّنة: 'الحكم لصالح المدعي'، 'مدان'."),
            ColumnDoc("summary", "TEXT", False, "ملخص سردي لحيثيات المحكمة وأسباب قرارها."),
            ColumnDoc("full_text_url", "VARCHAR(500)", True, "رابط النص الكامل للحكم، إن وُجد."),
        ],
    ),
    TableDoc(
        name="principles",
        description=(
            "المبادئ القانونية (قواعد قانونية أو مذاهب فقهية أو تسبيب مُنشئ لسابقة قضائية) "
            "التي أرساها أو طبّقها حكم ما، مصنّفة بحسب مجال القانون (area_of_law) مثل قانون "
            "العقود أو القانون الجنائي. استخدم هذا الجدول للإجابة عن 'ما المبدأ القانوني "
            "الذي أرسته هذه القضية' أو 'ابحث عن القضايا والأحكام التي أرست مبادئ في مجال "
            "قانوني معين'. ويُربط دائمًا بحكم واحد عبر judgement_id، ومنه إلى قضيته."
        ),
        primary_key="principle_id",
        foreign_keys=[ForeignKeyDoc("judgement_id", "judgements", "judgement_id")],
        columns=[
            ColumnDoc("principle_id", "INT", False, "معرّف فريد للمبدأ القانوني."),
            ColumnDoc("judgement_id", "INT", False, "الحكم الذي أرسى هذا المبدأ أو طبّقه. مفتاح خارجي إلى judgements.judgement_id."),
            ColumnDoc("title", "VARCHAR(255)", False, "الاسم المختصر للمبدأ، مثل 'جسامة الإخلال'. يُستخدم للعرض؛ للبحث في العنوان استعمل title_norm."),
            ColumnDoc("description", "TEXT", False, "شرح المبدأ القانوني وتسبيبه."),
            ColumnDoc("area_of_law", "VARCHAR(100)", False, "المجال القانوني الذي ينتمي إليه المبدأ. القيم المخزَّنة: 'قانون العقود'، 'القانون الجنائي'."),
            ColumnDoc("title_norm", "VARCHAR(255)", True, "نسخة مُوحَّدة إملائيًا من title يولّدها النظام تلقائيًا. استخدم هذا العمود عند البحث عن مبدأ بعنوانه."),
        ],
    ),
    TableDoc(
        name="legislations",
        description=(
            "التشريعات والأنظمة والقوانين المكتوبة التي يمكن أن تستند إليها القضايا، مثل "
            "'Financial Conduct Act' (قانون السلوك المالي) أو 'Commercial Contracts Act' "
            "(قانون العقود التجارية). استخدم هذا الجدول للإجابة عن الأسئلة المتعلقة بعنوان "
            "تشريع معين، أو نطاق سريانه، أو تاريخ سنّه، أو حالته الراهنة. ولمعرفة القضايا "
            "التي استندت إلى تشريع ما، اربط عبر case_legislation_citations."
        ),
        primary_key="legislation_id",
        columns=[
            ColumnDoc("legislation_id", "INT", False, "معرّف فريد للتشريع."),
            ColumnDoc("title", "VARCHAR(255)", False, "العنوان الرسمي للقانون أو النظام، مثل 'قانون السلوك المالي'. يُستخدم للعرض؛ للبحث في العنوان استعمل title_norm."),
            ColumnDoc("jurisdiction", "VARCHAR(255)", False, "النطاق الذي يسري عليه التشريع. القيم المخزَّنة: 'اتحادي'، 'محلي'."),
            ColumnDoc("enactment_date", "DATE", False, "تاريخ سنّ التشريع."),
            ColumnDoc("status", "ENUM('ساري','ملغى','معدل')", False, "الحالة القانونية الراهنة للتشريع. القيم المخزَّنة حصرًا: 'ساري' (ساري المفعول)، 'ملغى'، 'معدل'."),
            ColumnDoc("title_norm", "VARCHAR(255)", True, "نسخة مُوحَّدة إملائيًا من title يولّدها النظام تلقائيًا. استخدم هذا العمود عند البحث عن تشريع بعنوانه."),
        ],
    ),
    TableDoc(
        name="case_legislation_citations",
        description=(
            "جدول ربط يصل القضايا بالتشريعات التي استندت إليها أثناء نظرها، ويتضمن المادة "
            "أو البند المُستنَد إليه وسياق الاستناد. استخدم هذا الجدول للإجابة عن 'ما "
            "القوانين التي استندت إليها القضية الفلانية'، أو 'ما القضايا التي استندت إلى "
            "تشريع معين'، أو 'أي مادة من النظام استُند إليها ولماذا'. وهو محور الربط بين "
            "cases وlegislations."
        ),
        primary_key="citation_id",
        foreign_keys=[
            ForeignKeyDoc("case_id", "cases", "case_id"),
            ForeignKeyDoc("legislation_id", "legislations", "legislation_id"),
        ],
        columns=[
            ColumnDoc("citation_id", "INT", False, "معرّف فريد لسجل الاستناد."),
            ColumnDoc("case_id", "INT", False, "القضية التي استندت إلى التشريع. مفتاح خارجي إلى cases.case_id."),
            ColumnDoc("legislation_id", "INT", False, "التشريع المُستنَد إليه. مفتاح خارجي إلى legislations.legislation_id."),
            ColumnDoc("article_section", "VARCHAR(100)", True, "المادة أو البند المُستنَد إليه من التشريع، مثل 'المادة 12'."),
            ColumnDoc("citation_context", "TEXT", True, "شرح سبب الاستناد إلى التشريع في القضية وكيفيته."),
        ],
    ),
]


def build_table_doc_text(table: TableDoc) -> str:
    """Render a TableDoc into the retrieval-optimized text blob that gets embedded.

    Layout: description first (it carries the most retrieval signal), then a
    structured column list, primary key, and foreign keys the LLM needs to
    write correct JOINs. The labels stay English while the descriptions are
    Arabic — see this module's language policy.
    """
    lines = [
        f"Table: {table.name}",
        f"Description: {table.description}",
        "Columns:",
    ]
    for col in table.columns:
        nullability = "NULL" if col.nullable else "NOT NULL"
        lines.append(f"  - {col.name} ({col.type}, {nullability}): {col.description}")

    lines.append(f"Primary key: {table.primary_key}")

    if table.foreign_keys:
        lines.append("Foreign keys:")
        for fk in table.foreign_keys:
            lines.append(f"  - {table.name}.{fk.column} -> {fk.references_table}.{fk.references_column}")
    else:
        lines.append("Foreign keys: none")

    return "\n".join(lines)
