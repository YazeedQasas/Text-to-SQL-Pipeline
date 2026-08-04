"""
Palestinian legal-domain concepts for the legal_db demo database.

Why this file exists
--------------------
A judge asks in the vocabulary of the Palestinian court system. That vocabulary
is largely absent from the database: nothing anywhere stores the string
"محكمة الصلح", and no column marks a case as "متقادمة". The schema documents in
schema_docs.py describe what the columns ARE; this file describes what the user's
words MEAN in terms of those columns.

Each concept is embedded and stored as one Qdrant point in its own collection.
At query time the user's question is searched against it, and any concept that
clears the score threshold is added to the SQL prompt as a glossary line.

Field contract
--------------
- `term` / `aliases` / `definition` are the ONLY fields that get embedded (see
  `build_concept_doc_text`). They are what an Arabic question is matched
  against, so they should be written the way a judge or clerk would actually
  phrase things — not the way a database designer would.
- `sql` is NEVER embedded. Putting SQL in the embedded blob drags the vector
  away from the Arabic question it needs to match. It is carried in the payload
  and rendered into the prompt separately.
- `sql` is a FRAGMENT, never a complete query — a WHERE predicate, a computed
  expression, or a join path. A whole SELECT would be copied verbatim and the
  rest of the user's question ignored. Always write identifiers fully qualified
  (`courts.level`, not `level`) so the fragment names its own tables.
- `tables` does double duty: it biases schema retrieval toward the tables the
  concept needs, and it tells the model which tables a multi-table fragment
  spans. Every table named here must exist in schema_docs.py.

REVIEW REQUIRED
---------------
These entries are a starting set drafted from general knowledge of Palestinian
legal terminology. They need review by someone who practises in these courts
before the system is put in front of real users, because a wrong mapping here
produces a confident, plausible, wrong answer rather than an error.

Entries carrying a judgement call rather than a fact are marked inline:
`taqadum` (the limitation period is a policy number) and `qadaya_basita` (the
word has two readings and only one is mappable here).

Congestion concepts deliberately compute NOTHING. `db/05_congestion.sql` already
classifies pending cases against per-category thresholds held in
`congestion_rules`; those concepts only name the values it produces. Never
reimplement that policy here — it would silently compete with the real one.

Note also that this demo database models a generic court system, not the
Palestinian one — it has no محاكم شرعية and no دائرة تنفيذ. Concepts that would
need those are deliberately absent rather than mapped onto something
approximate.

BLOCKED ON schema_docs.py
-------------------------
`case_congestion`, `congestion_rules`, and the `cases.case_category` column are
NOT yet described in schema_docs.py, so schema retrieval cannot surface them.
Until they are, the congestion and category concepts will hand the model a
fragment referencing tables it cannot see, and it will invent something instead.
"""

from dataclasses import dataclass, field


@dataclass
class ConceptDoc:
    id: str
    term: str
    definition: str
    sql: str
    tables: list[str]
    aliases: list[str] = field(default_factory=list)


CONCEPTS: list[ConceptDoc] = [
    # --- Court vocabulary -----------------------------------------------------
    # The Palestinian regular-court hierarchy uses names that appear nowhere in
    # `courts.level`, whose stored values are only ابتدائية / استئناف / عليا.
    ConceptDoc(
        id="sulh_court",
        term="محكمة الصلح",
        aliases=["محاكم الصلح", "الصلح", "قاضي الصلح"],
        definition=(
            "محكمة الدرجة الأولى في النظام القضائي الفلسطيني، وتختص بنظر الدعاوى "
            "البسيطة والمخالفات والجنح الصغيرة والدعاوى المدنية محدودة القيمة. وهي "
            "أدنى درجات المحاكم النظامية، ويُستأنف حكمها أمام محكمة البداية بصفتها "
            "الاستئنافية."
        ),
        sql="courts.level = 'ابتدائية'",
        tables=["courts"],
    ),
    ConceptDoc(
        id="bidaya_court",
        term="محكمة البداية",
        aliases=["محاكم البداية", "البداية", "محكمة أول درجة"],
        definition=(
            "محكمة الدرجة الأولى التي تنظر في الدعاوى التي تخرج عن اختصاص محكمة "
            "الصلح، من الجنايات والدعاوى المدنية الكبيرة، وتنظر كذلك في استئناف "
            "أحكام محاكم الصلح. تُصنَّف في هذه البيانات ضمن المحاكم الابتدائية."
        ),
        sql="courts.level = 'ابتدائية'",
        tables=["courts"],
    ),
    ConceptDoc(
        id="isti2naf_court",
        term="محكمة الاستئناف",
        aliases=["محاكم الاستئناف", "الاستئناف", "محكمة الدرجة الثانية"],
        definition=(
            "المحكمة التي تنظر في الطعون المرفوعة على أحكام محاكم البداية، وتعيد "
            "بحث الدعوى في الوقائع والقانون معًا. وهي الدرجة الثانية من درجات "
            "التقاضي."
        ),
        sql="courts.level = 'استئناف'",
        tables=["courts"],
    ),
    ConceptDoc(
        id="naqd_court",
        term="محكمة النقض",
        aliases=["النقض", "محكمة التمييز", "التمييز", "المحكمة العليا", "أعلى درجة"],
        definition=(
            "أعلى درجات المحاكم النظامية، وتنظر في الطعون على أحكام محاكم الاستئناف "
            "من حيث صحة تطبيق القانون لا من حيث الوقائع. وهي المرجع الذي تصدر عنه "
            "المبادئ القانونية المُلزمة لما دونها من المحاكم."
        ),
        sql="courts.level = 'عليا'",
        tables=["courts"],
    ),
    # --- Party vocabulary -----------------------------------------------------
    # المستدعي / المستدعى ضده are the procedural terms in common Palestinian and
    # Jordanian usage; the database stores only مدعي / مدعى عليه.
    ConceptDoc(
        id="mustadei",
        term="المستدعي",
        aliases=["المدعي", "الجهة المستدعية", "طالب الدعوى", "المشتكي", "رافع الدعوى"],
        definition=(
            "الطرف الذي بادر برفع الدعوى وطلب من المحكمة الحكم له بحقٍّ يدّعيه قِبَل "
            "خصمه. ويُسمّى في الاصطلاح الإجرائي الفلسطيني المستدعي أو المدعي، وهو "
            "صاحب الصفة في تحريك الدعوى."
        ),
        sql="case_parties.role = 'مدعي'",
        tables=["case_parties", "parties"],
    ),
    ConceptDoc(
        id="mustadaa_diddahu",
        term="المستدعى ضده",
        aliases=["المدعى عليه", "المشتكى عليه", "الخصم", "الطرف المُدَّعى عليه"],
        definition=(
            "الطرف الذي رُفعت الدعوى في مواجهته والمطلوب منه الرد على ادعاءات "
            "المستدعي. ويُسمّى المستدعى ضده أو المدعى عليه، وفي الدعاوى الجزائية "
            "المشتكى عليه."
        ),
        sql="case_parties.role = 'مدعى عليه'",
        tables=["case_parties", "parties"],
    ),
    ConceptDoc(
        id="wakil",
        term="الوكيل",
        aliases=["وكيل الخصم", "وكيل الطرف", "المحامي الوكيل", "التوكيل", "من يمثل الطرف"],
        definition=(
            "المحامي الذي ينوب عن أحد أطراف الدعوى ويمثّله أمام المحكمة بموجب وكالة. "
            "ويُسأل عنه عادةً بصيغة 'من يمثل فلانًا' أو 'من وكيل المدعى عليه'، "
            "والوكالة مسجَّلة على مستوى القضية والطرف معًا لا على مستوى المحامي وحده."
        ),
        sql="case_parties.lawyer_id = lawyers.lawyer_id",
        tables=["case_parties", "lawyers", "parties"],
    ),
    # --- Case-status vocabulary ----------------------------------------------
    ConceptDoc(
        id="dawa_mashtuba",
        term="الدعوى المشطوبة",
        aliases=["شطب الدعوى", "القضايا المشطوبة", "الشطب", "دعوى مشطوبة"],
        definition=(
            "الدعوى التي قررت المحكمة شطبها من سجلاتها دون الفصل في موضوعها، لأسبابٍ "
            "إجرائية مثل تخلّف المدعي عن الحضور أو عدم متابعته لدعواه. والشطب لا "
            "يمنع تجديد الدعوى، لكنه ينهي النظر فيها بصيغتها القائمة."
        ),
        sql="cases.status = 'مشطوبة'",
        tables=["cases"],
    ),
    ConceptDoc(
        id="hukm_qat3i",
        term="الحكم القطعي",
        aliases=["الحكم البات", "الحكم النهائي", "الدعوى المحسومة", "اكتساب الدرجة القطعية"],
        definition=(
            "الحكم الذي استنفد طرق الطعن العادية فأصبح باتًّا وواجب التنفيذ، ولم يعد "
            "قابلًا للاستئناف. ويقابله في هذه البيانات القضية التي أُغلقت وصدر فيها "
            "حكم، بخلاف القضية التي ما زالت قيد الاستئناف."
        ),
        sql=(
            "cases.status = 'مغلقة' AND EXISTS "
            "(SELECT 1 FROM judgements WHERE judgements.case_id = cases.case_id)"
        ),
        tables=["cases", "judgements"],
    ),
    # --- Derived concepts: rules with no column of their own -------------------
    ConceptDoc(
        id="taqadum",
        term="الدعوى المتقادمة",
        aliases=["التقادم", "مرور الزمن", "سقوط الحق بالتقادم", "الدعاوى القديمة"],
        # PARKED — NOT VALIDATED. 15 years is the general limitation period for
        # personal rights, but the period differs by claim type (commercial,
        # labour, tort) and no column here records which regime a case falls
        # under. A six-month reading was tried and reverted: it collided with the
        # congestion model, which owns "pending too long" per category.
        #
        # This concept currently matches ZERO rows in the demo data (no open case
        # is 15 years old), so it cannot be tested end to end. Settle the period
        # with a reviewer before relying on it.
        definition=(
            "الدعوى التي مضت على نشوء الحق فيها المدة التي يسقط بعدها حق المطالبة "
            "القضائية، فيجوز للخصم الدفع بمرور الزمن. والمدة العامة في الحقوق "
            "الشخصية خمس عشرة سنة ما لم ينص القانون على مدة أخص."
        ),
        sql=(
            "cases.status = 'مفتوحة' "
            "AND cases.filing_date < DATE_SUB(CURDATE(), INTERVAL 15 YEAR)"
        ),
        tables=["cases"],
    ),
    # --- Judicial congestion --------------------------------------------------
    # These do NOT compute anything. `case_congestion` (db/05_congestion.sql)
    # already classifies every pending case against a per-category threshold
    # held in `congestion_rules`. The job here is only to connect the words a
    # judge uses to the values that view already produces — an earlier draft of
    # this file invented its own "no hearing in 180 days" rule, which competed
    # with the real policy and was removed.
    #
    # The view covers PENDING cases only (status IN 'مفتوحة','قيد الاستئناف'),
    # so every congestion concept is implicitly about unresolved cases. Its
    # `congestion_status` takes exactly four values: مختنقة، قيد المراقبة،
    # ضمن المدة الطبيعية، غير مصنفة.
    ConceptDoc(
        id="qadiya_mukhtaniqa",
        term="القضية المختنقة",
        aliases=[
            "مختنقة",
            "القضايا المختنقة",
            "الاختناق القضائي",
            "القضايا المتراكمة",
            "تجاوزت المدة",
        ],
        definition=(
            "الدعوى التي ما زالت منظورة وتجاوزت المدة القصوى المقررة للفصل في "
            "مثلها بحسب تصنيفها، فصارت في عداد القضايا المتراكمة. والمدة تختلف "
            "باختلاف نوع القضية، فما يُعد اختناقًا في المخالفات والجنح البسيطة لا "
            "يُعد كذلك في الجنايات الكبرى أو قضايا المواريث."
        ),
        sql="case_congestion.congestion_status = 'مختنقة'",
        tables=["case_congestion"],
    ),
    ConceptDoc(
        id="qadiya_qaid_almuraqaba",
        term="القضية قيد المراقبة",
        aliases=["قيد المراقبة", "القضايا تحت المراقبة", "اقتربت من التجاوز", "منذرة بالاختناق"],
        definition=(
            "الدعوى التي تجاوزت مدة التنبيه المقررة لصنفها لكنها لم تبلغ بعد حد "
            "الاختناق، فهي في المنطقة الوسطى التي تستوجب المتابعة الإدارية قبل أن "
            "تتحول إلى قضية متراكمة."
        ),
        sql="case_congestion.congestion_status = 'قيد المراقبة'",
        tables=["case_congestion"],
    ),
    ConceptDoc(
        id="muddat_alfasl_almuqarrara",
        term="المدة المقررة للفصل",
        aliases=[
            "عتبة الاختناق",
            "المدة القصوى",
            "الحد الزمني للقضية",
            "لماذا اعتُبرت مختنقة",
            "سبب الاختناق",
        ],
        definition=(
            "المدة الزمنية المعيارية المقررة لكل صنف من أصناف القضايا، وتنقسم إلى "
            "مدة تنبيه تدخل بعدها القضية تحت المراقبة، ومدة قصوى تُعد بعدها مختنقة. "
            "ولكل صنف مبرره الذي يشرح سبب تحديد مدته على هذا النحو."
        ),
        sql="congestion_rules.watch_months, congestion_rules.congested_months, congestion_rules.rationale",
        tables=["congestion_rules"],
    ),
    # --- Case-category vocabulary ---------------------------------------------
    ConceptDoc(
        id="qadaya_basita",
        term="القضايا البسيطة",
        aliases=["بسيطة", "المخالفات والجنح البسيطة", "الدعاوى الصغيرة", "المخالفات"],
        # AMBIGUITY — REVIEW. "بسيطة" can mean the formal category below, or
        # loosely "a case within محكمة الصلح jurisdiction" (a value threshold).
        # This database has no claim-value column, so only the formal category
        # is mappable. If users mean the jurisdictional sense, that needs a
        # separate concept pointing at courts.level.
        definition=(
            "صنف القضايا محدود الخطورة والإجراءات، ويشمل المخالفات والجنح الصغيرة "
            "مثل مخالفات السير والمشاجرات البسيطة والشيكات بدون رصيد. وهي أقصر "
            "الأصناف مدةً في الفصل، إذ تكفيها إجراءات تحقيق مبسطة وجلسات مرافعة "
            "محدودة."
        ),
        sql="cases.case_category = 'مخالفات وجنح بسيطة'",
        tables=["cases"],
    ),
    ConceptDoc(
        id="qadaya_mudawwara",
        term="القضايا المدورة",
        aliases=["مدورة", "القضايا المرحّلة", "المدور من السنوات السابقة", "الرصيد المدور"],
        definition=(
            "القضايا التي رُفعت في سنة سابقة وما زالت منظورة لم يُفصل فيها، فتُرحّل "
            "إلى السنة الجديدة وتُضاف إلى رصيد المحكمة المدور. ويقابلها القضايا "
            "الواردة، وهي المرفوعة خلال السنة الجارية نفسها."
        ),
        sql=(
            "cases.status IN ('مفتوحة', 'قيد الاستئناف') "
            "AND YEAR(cases.filing_date) < YEAR(CURDATE())"
        ),
        tables=["cases"],
    ),
    ConceptDoc(
        id="qadaya_warida",
        term="القضايا الواردة",
        aliases=["الواردة", "القضايا الجديدة", "المرفوعة هذا العام", "الوارد السنوي"],
        definition=(
            "القضايا التي رُفعت أمام المحكمة خلال السنة الجارية، بخلاف المدورة "
            "المرحّلة من السنوات السابقة. وتُستخدم مع المدورة لقياس حركة المحكمة "
            "ومعدل إنجازها خلال السنة."
        ),
        sql="YEAR(cases.filing_date) = YEAR(CURDATE())",
        tables=["cases"],
    ),
    ConceptDoc(
        id="amad_taqadi",
        term="أمد التقاضي",
        aliases=["مدة الفصل في الدعوى", "طول أمد التقاضي", "كم استغرقت القضية", "زمن البت"],
        definition=(
            "المدة الزمنية التي استغرقتها الدعوى من تاريخ رفعها أمام المحكمة حتى "
            "تاريخ صدور الحكم الفاصل فيها، وتُحتسب بالأيام. وهي من أهم مؤشرات قياس "
            "أداء المحاكم وسرعة إنجازها للقضايا."
        ),
        sql="DATEDIFF(judgements.decision_date, cases.filing_date)",
        tables=["cases", "judgements"],
    ),
    # --- Doctrine and legislation ---------------------------------------------
    ConceptDoc(
        id="sabiqa_qadaiya",
        term="السابقة القضائية",
        aliases=["المبدأ القانوني", "الاجتهاد القضائي", "المبادئ المستقرة", "قضاء محكمة النقض"],
        definition=(
            "القاعدة القانونية التي أرساها حكم قضائي سابق في مسألة معينة، فصارت "
            "مرجعًا يُستأنس به أو يُلتزم به في القضايا المماثلة. وتصدر المبادئ "
            "المستقرة عادةً عن أعلى درجات التقاضي، وترتبط دائمًا بالحكم الذي "
            "أرساها وبالقضية التي صدر فيها."
        ),
        sql="principles.judgement_id = judgements.judgement_id",
        tables=["principles", "judgements", "cases"],
    ),
    ConceptDoc(
        id="tashri_sari",
        term="التشريع الساري",
        aliases=["القانون النافذ", "القوانين السارية", "ساري المفعول", "التشريعات المعمول بها"],
        definition=(
            "القانون أو النظام الذي ما زال معمولًا به ولم يُلغَ، فتصح الإحالة إليه "
            "والاستناد إليه في الأحكام. ويقابله القانون الملغى الذي انتهى العمل به، "
            "والقانون المعدَّل الذي سرى عليه تعديل لاحق."
        ),
        sql="legislations.status = 'ساري'",
        tables=["legislations"],
    ),
]


def build_concept_doc_text(concept: ConceptDoc) -> str:
    """Render a ConceptDoc into the text blob that gets embedded.

    Deliberately excludes `sql` and `tables`. The blob is matched against an
    Arabic question, so it holds only the Arabic surface forms a user might
    type — the term, the ways it is otherwise said, and what it means. Adding
    the SQL fragment here would pull the vector toward identifier soup and away
    from the question.

    Aliases come before the definition because they carry more matching signal
    per token: a user asking "كم قضية أمام محاكم الصلح" has written an alias
    almost verbatim, while the definition matches only loosely.

    PENDING EXPERIMENT — add a sample question per concept.
    Measured against a 13-question probe, true matches bottom out at 0.5605
    (محاكم الصلح) while the worst false positive reaches 0.5149 ("ما هي عناوين
    القضايا المدنية؟" pulling القضايا البسيطة). A 0.046 margin is too thin to
    trust. A question embeds closer to another question than a definition does,
    so adding one sample question per concept to this blob should lift the true
    matches without lifting the false ones. Measure the gap before and after —
    if it does not widen, drop the idea rather than carrying the extra field.
    """
    lines = [f"المصطلح: {concept.term}"]
    if concept.aliases:
        lines.append(f"ويُسمّى أيضًا: {'، '.join(concept.aliases)}")
    lines.append(f"التعريف: {concept.definition}")
    return "\n".join(lines)
