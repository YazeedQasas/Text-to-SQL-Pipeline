import { useEffect, useRef, useState } from "react";
import { fetchLiveConcepts, saveConcept } from "./api";

/**
 * The legal glossary as Qdrant holds it, edited in place.
 *
 * This lists the *live* collection rather than `data/concepts.json`, because
 * Qdrant is what the query pipeline reads: a definition only changes an answer
 * once it is a point in that collection. Saving writes there first and mirrors
 * the result into the file afterwards, so the file is a backup of what is in
 * force and never the thing standing between an edit and its effect.
 *
 * Creating and editing are the same write — an upsert keyed by the concept id —
 * which is why one form serves both.
 */

/** Comma-or-Arabic-comma separated text -> a list, and back. */
function splitList(text) {
  return text
    .split(/[,،\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

const BLANK = { id: "", term: "", aliases: [], definition: "", sql: "", tables: [] };

/** Cards on screen at first, and how many each "show more" press adds. */
const PAGE_SIZE = 5;

export default function ConceptsPanel() {
  const [concepts, setConcepts] = useState([]);
  const [count, setCount] = useState(0);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  // The id being edited, or "__new__" for the creation form. One at a time:
  // two open forms could each save over the other's concept.
  const [editing, setEditing] = useState(null);
  const [saved, setSaved] = useState(null);
  // How many cards are drawn. A glossary of any size otherwise arrives as one
  // wall of definitions, and the search below is the thing you actually want.
  const [shown, setShown] = useState(PAGE_SIZE);

  async function load() {
    try {
      const data = await fetchLiveConcepts();
      setConcepts(data.concepts);
      setCount(data.qdrant_count);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function handleSave(concept) {
    await saveConcept(concept);
    setEditing(null);
    setSaved(concept.id);
    await load();
  }

  function download() {
    const body = JSON.stringify({ concepts }, null, 2);
    const url = URL.createObjectURL(new Blob([body], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "concepts.json";
    link.click();
    URL.revokeObjectURL(url);
  }

  const needle = query.trim().toLowerCase();
  const visible = needle
    ? concepts.filter((concept) =>
        [concept.id, concept.term, concept.definition, ...(concept.aliases || [])]
          .join(" ")
          .toLowerCase()
          .includes(needle),
      )
    : concepts;
  const listed = visible.slice(0, shown);

  return (
    <div className="concepts">
      <p className="subtitle">
        المصطلحات التي يستخدمها القاضي ولا تخزّنها قاعدة البيانات مباشرة، وكل مصطلح
        مرتبط بالشرط الذي يعبّر عنه. ما تراه هنا هو المفاهيم المفعّلة فعليًا، وأي حفظ
        يسري مفعوله فورًا.
      </p>

      <div className="catalog-actions">
        <button
          type="button"
          className="approve"
          onClick={() => {
            setEditing("__new__");
            setSaved(null);
          }}
          disabled={editing === "__new__"}
        >
          ادخل مفهوم جديد
        </button>
        <button type="button" onClick={download} disabled={concepts.length === 0}>
          تنزيل نسخة احتياطية
        </button>
      </div>

      {!loading && (
        <p className="catalog-summary">{count} مفهومًا مفعّلًا</p>
      )}

      {error && <div className="error">{error}</div>}

      {editing === "__new__" && (
        <ConceptForm
          concept={BLANK}
          isNew
          onSave={handleSave}
          onCancel={() => setEditing(null)}
        />
      )}

      {concepts.length > 0 && (
        <input
          type="search"
          className="concepts-search"
          placeholder="ابحث في المفاهيم…"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            // A new search is a new list; carrying the old window over would
            // show its first 20 hits and hide the rest behind a button that
            // looks like it was already pressed.
            setShown(PAGE_SIZE);
          }}
        />
      )}

      {loading && <p className="catalog-hint">جارٍ التحميل…</p>}

      {!loading && concepts.length === 0 && !error && (
        <p className="activity-empty">لا توجد مفاهيم بعد.</p>
      )}

      <ul className="concepts-list">
        {listed.map((concept) =>
          editing === concept.id ? (
            <li key={concept.id}>
              <ConceptForm
                concept={concept}
                onSave={handleSave}
                onCancel={() => setEditing(null)}
              />
            </li>
          ) : (
            <li key={concept.id} className="concept-card">
              <div className="concept-head">
                <span className="concept-term">{concept.term}</span>
                <code className="concept-id">{concept.id}</code>
              </div>
              {concept.aliases?.length > 0 && (
                <div className="concept-aliases">{concept.aliases.join("، ")}</div>
              )}
              <p className="concept-definition">{concept.definition}</p>
              <code className="concept-sql">{concept.sql}</code>
              {concept.tables?.length > 0 && (
                <div className="concept-tables">{concept.tables.join(" · ")}</div>
              )}
              <div className="concept-actions">
                <button
                  type="button"
                  className="activity-toggle"
                  onClick={() => {
                    setEditing(concept.id);
                    setSaved(null);
                  }}
                >
                  تعديل
                </button>
                {saved === concept.id && (
                  <span className="doc-editor-saved">تم الحفظ.</span>
                )}
              </div>
            </li>
          ),
        )}
      </ul>

      {visible.length > listed.length && (
        <div className="concepts-more">
          <button type="button" onClick={() => setShown((n) => n + PAGE_SIZE)}>
            عرض المزيد
          </button>
          {/* Where you are in the list, so the button says how much is left
              rather than just that something is. */}
          <span className="catalog-hint">
            {listed.length} من {visible.length}
          </span>
        </div>
      )}
    </div>
  );
}

/**
 * The edit/create form.
 *
 * The id is fixed once a concept exists: it is the Qdrant point key, so letting
 * it be retyped would create a second concept rather than rename the one on
 * screen — and leave the original in place, still answering questions.
 */
function ConceptForm({ concept, isNew = false, onSave, onCancel }) {
  const [draft, setDraft] = useState(concept);
  const [aliasText, setAliasText] = useState((concept.aliases || []).join("، "));
  const [tableText, setTableText] = useState((concept.tables || []).join("، "));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const formRef = useRef(null);

  // The widget scrolls inside itself, so a form opened from a card halfway down
  // the list lands mostly below the fold — including its save button, which
  // reads as the form not having opened at all.
  useEffect(() => {
    formRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, []);

  function set(field, value) {
    setDraft((prev) => ({ ...prev, [field]: value }));
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await onSave({
        ...draft,
        id: draft.id.trim(),
        aliases: splitList(aliasText),
        tables: splitList(tableText),
      });
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  const ready = draft.id.trim() && draft.term.trim() && draft.definition.trim();

  return (
    <form className="concept-form" ref={formRef} onSubmit={handleSubmit}>
      <div className="concept-form-title">{isNew ? "مفهوم جديد" : `تعديل: ${concept.term}`}</div>

      <label className="field">
        <span>المعرّف (بالإنجليزية، بدون مسافات)</span>
        <input
          type="text"
          dir="ltr"
          value={draft.id}
          disabled={!isNew}
          onChange={(event) => set("id", event.target.value)}
          placeholder="congested_case"
        />
      </label>

      <label className="field">
        <span>المصطلح</span>
        <input
          type="text"
          value={draft.term}
          onChange={(event) => set("term", event.target.value)}
          placeholder="القضية المختنقة"
        />
      </label>

      <label className="field">
        <span>مرادفات (افصل بينها بفاصلة)</span>
        <input
          type="text"
          value={aliasText}
          onChange={(event) => setAliasText(event.target.value)}
          placeholder="مختنقة، متعثّرة"
        />
      </label>

      <label className="field">
        <span>التعريف (هذا ما تتم مطابقة الأسئلة معه)</span>
        <textarea
          rows={3}
          value={draft.definition}
          onChange={(event) => set("definition", event.target.value)}
          placeholder="اشرح ما يعنيه المصطلح كما يقوله القاضي."
        />
      </label>

      <label className="field">
        <span>الشرط المرتبط بالمصطلح</span>
        <textarea
          rows={2}
          dir="ltr"
          value={draft.sql}
          onChange={(event) => set("sql", event.target.value)}
          placeholder="case_congestion.congestion_status = 'مختنقة'"
        />
      </label>

      <label className="field">
        <span>الجداول المرتبطة (افصل بينها بفاصلة)</span>
        <input
          type="text"
          dir="ltr"
          value={tableText}
          onChange={(event) => setTableText(event.target.value)}
          placeholder="case_congestion"
        />
      </label>

      {error && <div className="error">{error}</div>}

      <div className="doc-editor-footer">
        <button type="submit" className="approve" disabled={saving || !ready}>
          {saving ? "جارٍ الحفظ…" : "حفظ"}
        </button>
        <button type="button" className="dismiss" onClick={onCancel} disabled={saving}>
          إلغاء
        </button>
        {!ready && <span className="catalog-hint">المعرّف والمصطلح والتعريف مطلوبة.</span>}
      </div>
    </form>
  );
}
