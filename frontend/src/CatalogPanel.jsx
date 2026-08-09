import { useEffect, useState } from "react";
import { approveCatalog, dismissReview, fetchReviewQueue, scanCatalog } from "./api";

const SEVERITY_LABELS = {
  ok: "يبدو سليمًا",
  thin_description: "الوصف غير كافٍ",
  off_domain: "لا يخصّ هذا المجال",
  unintelligible: "غير مفهوم",
};

const CHANGE_LABELS = {
  table_added: "جدول جديد",
  table_dropped: "جدول محذوف من قاعدة البيانات",
  table_modified: "تغيّر في البنية",
};

/** Dropped tables default to being deleted from the index; everything else to being synced. */
function defaultAction(change) {
  return change.change_type === "table_dropped" ? "delete" : "upsert";
}

/** A card with no description yet is the one thing still worth stopping for. */
function missingDescription(item) {
  return item.action === "upsert" && !item.doc.description.trim();
}

export default function CatalogPanel({ onReviewCountChange }) {
  const [items, setItems] = useState(null); // null = never scanned
  const [scanning, setScanning] = useState(false);
  const [approving, setApproving] = useState(false);
  const [error, setError] = useState(null);
  const [summary, setSummary] = useState(null);
  const [approved, setApproved] = useState(null);
  // Everything the automated path documented — both the tables it flagged and
  // the ones it indexed on its own. Same shape as a scan result, so they render
  // through the same card.
  const [pending, setPending] = useState([]);
  const [savingPending, setSavingPending] = useState(false);

  async function loadPending() {
    try {
      const data = await fetchReviewQueue();
      const entries = data.entries.map((entry) => ({ ...entry, action: defaultAction(entry) }));
      setPending(entries);
      // Only the not-yet-indexed ones get badged: an already-indexed table
      // works, so nagging about it would train people to ignore the badge.
      onReviewCountChange?.(entries.filter((entry) => !entry.indexed).length);
    } catch {
      // The review queue is supplementary; failing to load it must not stop
      // the manual scan flow, which is the reason this panel exists.
    }
  }

  useEffect(() => {
    loadPending();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function updateItem(tableName, patch) {
    setItems((prev) =>
      prev.map((item) =>
        item.table_name === tableName
          ? { ...item, ...(typeof patch === "function" ? patch(item) : patch) }
          : item,
      ),
    );
  }

  function updatePending(tableName, patch) {
    setPending((prev) =>
      prev.map((item) =>
        item.table_name === tableName
          ? { ...item, ...(typeof patch === "function" ? patch(item) : patch) }
          : item,
      ),
    );
  }

  async function handleApprovePending() {
    const actionable = pending.filter((item) => item.action !== "skip");
    if (actionable.length === 0) return;

    setSavingPending(true);
    setError(null);
    try {
      // The same endpoint the manual flow uses, which also clears these from
      // the review queue — once a person has saved it, it has been seen.
      await approveCatalog(actionable.map((item) => ({ doc: item.doc, action: item.action })));
      await loadPending();
    } catch (err) {
      setError(err.message);
    } finally {
      setSavingPending(false);
    }
  }

  async function handleDismiss(tableName) {
    setError(null);
    try {
      await dismissReview(tableName);
      await loadPending();
    } catch (err) {
      setError(err.message);
    }
  }

  async function handleScan() {
    setScanning(true);
    setError(null);
    setApproved(null);
    setItems(null);
    setSummary(null);

    try {
      const data = await scanCatalog();
      setItems(
        data.changes.map((change) => ({ ...change, action: defaultAction(change) })),
      );
      setSummary({
        indexed: data.indexed_table_count,
        live: data.live_table_count,
        changed: data.changes.length,
      });
    } catch (err) {
      setError(err.message);
    } finally {
      setScanning(false);
    }
  }

  async function handleApprove() {
    setApproving(true);
    setError(null);
    try {
      const result = await approveCatalog(
        items.map((item) => ({ doc: item.doc, action: item.action })),
      );
      setApproved(result);
      setItems(null);
      setSummary(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setApproving(false);
    }
  }

  const blockedCount = items?.filter(missingDescription).length ?? 0;
  const actionableCount = items?.filter((item) => item.action !== "skip").length ?? 0;

  return (
    <div className="catalog">
      <p className="subtitle">
        تحقّق ممّا إذا تغيّرت قاعدة البيانات منذ آخر بناء للفهرس، وراجع كل ما هو جديد
        قبل إضافته.
      </p>

      <div className="catalog-actions">
        <button type="button" className="approve" onClick={handleScan} disabled={scanning || approving}>
          {scanning ? "جارٍ الفحص…" : "ابحث"}
        </button>
        {scanning && (
          <span className="catalog-hint">
            قراءة البنية وأخذ عيّنات من الصفوف ومراجعة كل تغيير — يستغرق ذلك لحظات.
          </span>
        )}
      </div>

      {error && <div className="error">{error}</div>}

      {approved && (
        <div className="catalog-approved">
          <strong>تم تحديث الفهرس.</strong>
          {approved.upserted.length > 0 && <div>أُضيف للفهرس: {approved.upserted.join("، ")}</div>}
          {approved.deleted.length > 0 && <div>حُذف: {approved.deleted.join("، ")}</div>}
          {approved.skipped.length > 0 && <div>تُرك كما هو: {approved.skipped.join("، ")}</div>}
        </div>
      )}

      {summary && (
        <p className="catalog-summary">
          {summary.changed === 0
            ? `لا تغييرات. ${summary.live} جدولًا في قاعدة البيانات و${summary.indexed} في الفهرس — كلها متطابقة.`
            : `تم العثور على ${summary.changed} تغييرًا ضمن ${summary.live} جدولًا.`}
        </p>
      )}

      {items?.map((item) => (
        <ChangeCard
          key={item.table_name}
          item={item}
          onChange={(patch) => updateItem(item.table_name, patch)}
        />
      ))}

      {items?.length > 0 && (
        <div className="catalog-footer">
          <button
            type="button"
            className="approve"
            onClick={handleApprove}
            disabled={approving || blockedCount > 0 || actionableCount === 0}
          >
            {approving ? "جارٍ التحديث…" : "اعتمد وحدّث الفهرس"}
          </button>
          {blockedCount > 0 && (
            <span className="catalog-hint">
              {blockedCount} جدولًا ما زال بحاجة إلى وصف.
            </span>
          )}
        </div>
      )}

      {pending.length > 0 && (
        <section className="review-queue">
          <h3 className="review-queue-title">
            {pending.length} جدولًا تم توثيقه تلقائيًا
          </h3>
          <p className="catalog-hint">
            التُقطت من تغيير في قاعدة البيانات ووصفها النموذج. اقرأ وصف الجدول ووصف
            الأعمدة أدناه — فهي ما تتم مطابقة الأسئلة معه، والوصف المبهم هنا يعني
            إجابات ضعيفة. عدّل ما تشاء ثم أعد الفهرسة.
          </p>

          {pending.map((item) => (
            <ChangeCard
              key={item.table_name}
              item={item}
              onChange={(patch) => updatePending(item.table_name, patch)}
              onDismiss={() => handleDismiss(item.table_name)}
              detectedAt={item.detected_at}
              indexed={item.indexed}
            />
          ))}

          <div className="catalog-footer">
            <button
              type="button"
              className="approve"
              onClick={handleApprovePending}
              disabled={
                savingPending ||
                pending.filter((item) => item.action !== "skip").length === 0 ||
                pending.some(missingDescription)
              }
            >
              {savingPending ? "جارٍ الحفظ…" : "احفظ وأعد الفهرسة"}
            </button>
            {pending.some(missingDescription) && (
              <span className="catalog-hint">
                {pending.filter(missingDescription).length} ما زال بحاجة إلى وصف.
              </span>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

function ChangeCard({ item, onChange, onDismiss, detectedAt, indexed }) {
  const { verdict, doc } = item;
  // The scan's verdict is shown for information only — it never blocks a sync.
  const flagged = verdict.needs_edit;
  // Review-queue entries arrive with descriptions already written, and reading
  // them is the whole point of the card, so the columns start open. A scan
  // result's columns are usually empty and would just be noise expanded.
  const isReviewEntry = indexed !== undefined;

  function setDescription(description) {
    onChange({ doc: { ...doc, description } });
  }

  function setColumnDescription(name, description) {
    onChange({
      doc: {
        ...doc,
        columns: doc.columns.map((column) =>
          column.name === name ? { ...column, description } : column,
        ),
      },
    });
  }

  return (
    <div className={`change-card${flagged ? " flagged" : ""}`}>
      <div className="change-header">
        <div>
          <strong dir="ltr">{item.table_name}</strong>
          <span className="change-type">{CHANGE_LABELS[item.change_type] ?? item.change_type}</span>
        </div>
        <div className="change-badges">
          {isReviewEntry && (
            <span className={`badge badge-${indexed ? "live" : "warn"}`}>
              {indexed ? "قابل للاستعلام الآن" : "لم يُفهرس بعد"}
            </span>
          )}
          <span className={`badge badge-${flagged ? "warn" : "ok"}`}>
            {SEVERITY_LABELS[verdict.severity] ?? verdict.severity}
          </span>
        </div>
      </div>

      <p className="change-summary" dir="auto">
        {item.summary}
        {detectedAt > 0 && (
          <span className="change-detected">
            {" "}
            · التُقط في {new Date(detectedAt * 1000).toLocaleString("ar")}
          </span>
        )}
      </p>

      {flagged && verdict.reasons.length > 0 && (
        <ul className="change-reasons">
          {verdict.reasons.map((reason, index) => (
            <li key={index} dir="auto">
              {reason}
            </li>
          ))}
        </ul>
      )}

      {item.action !== "delete" && (
        <>
          <label className="field">
            <span>الوصف (هذا ما يتم البحث فيه)</span>
            <textarea
              rows={3}
              value={doc.description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="صف ما يحتويه هذا الجدول وعلاقته ببقية الجداول."
            />
          </label>

          {verdict.suggested_description && verdict.suggested_description !== doc.description && (
            <div className="suggestion">
              <span dir="auto">مقترح: {verdict.suggested_description}</span>
              <button type="button" onClick={() => setDescription(verdict.suggested_description)}>
                استخدم هذا
              </button>
            </div>
          )}

          <details className="change-columns" open={isReviewEntry}>
            <summary>
              وصف الأعمدة ({doc.columns.length})
              {isReviewEntry &&
                doc.columns.some((column) => !column.description.trim()) &&
                ` — ${doc.columns.filter((column) => !column.description.trim()).length} فارغ`}
            </summary>
            {doc.columns.map((column) => (
              <label key={column.name} className="field field-inline">
                <span dir="ltr">
                  {column.name} <em>{column.type}</em>
                </span>
                <input
                  type="text"
                  value={column.description}
                  onChange={(e) => setColumnDescription(column.name, e.target.value)}
                  placeholder="ماذا يحتوي هذا العمود؟"
                />
              </label>
            ))}
          </details>
        </>
      )}

      {item.sample_rows.length > 0 && (
        <details className="change-sample">
          <summary>
            عيّنة من الصفوف ({item.sample_rows.length} من {item.row_count})
          </summary>
          <pre dir="ltr">{JSON.stringify(item.sample_rows, null, 2)}</pre>
        </details>
      )}

      <div className="change-controls">
        <label>
          <input
            type="radio"
            checked={item.action !== "skip"}
            onChange={() => onChange({ action: defaultAction(item) })}
          />
          {item.change_type === "table_dropped"
            ? "احذف من الفهرس"
            : indexed
              ? "احفظ تعديلاتي"
              : "أضف إلى الفهرس"}
        </label>
        <label>
          <input
            type="radio"
            checked={item.action === "skip"}
            onChange={() => onChange({ action: "skip" })}
          />
          اتركه كما هو
        </label>
        {onDismiss && (
          // Review-queue entries only. "Leave alone" keeps it in the list for
          // next time; this takes it off the list for good without changing
          // what is indexed either way.
          <button type="button" className="dismiss" onClick={onDismiss}>
            {indexed ? "يبدو جيدًا، أخفِه" : "تجاهل"}
          </button>
        )}
      </div>
    </div>
  );
}
