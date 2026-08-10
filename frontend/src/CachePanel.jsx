import { useCallback, useEffect, useState } from "react";
import { clearQueryCache, fetchQueryCache, removeCachedQuestion } from "./api";

/**
 * The repeated-question cache, and whether it is earning its place.
 *
 * The number this panel exists for is the hit rate. Everything else here is
 * context for it: a cache with entries is not the same as a cache that is
 * working, and the only way to tell them apart is how often a stored question
 * gets asked a second time.
 *
 * Entries are shown as the question and the SQL, not as the key they are stored
 * under. The key is a hash — reading it tells nobody anything, and the point of
 * looking at this list is to spot a stored query that answers its question
 * wrongly, which needs both halves side by side.
 */

function formatTime(ts) {
  if (!ts) return "—";
  const date = new Date(ts * 1000);
  const sameDay = date.toDateString() === new Date().toDateString();
  return sameDay
    ? date.toLocaleTimeString("ar", { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleString("ar", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export default function CachePanel() {
  const [stats, setStats] = useState(null);
  const [entries, setEntries] = useState([]);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  // Two-step, like the activity log's clear: it cannot be undone, and a browser
  // confirm() is both easy to dismiss reflexively and impossible to style.
  const [confirmingClear, setConfirmingClear] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await fetchQueryCache(200);
      setStats(data.stats);
      setEntries(data.entries);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleClear() {
    setBusy(true);
    try {
      await clearQueryCache();
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
      setConfirmingClear(false);
    }
  }

  async function handleRemove(key) {
    setBusy(true);
    try {
      await removeCachedQuestion(key);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const asked = stats ? stats.hits + stats.misses : 0;

  return (
    <div className="cache">
      <p className="subtitle">
        الأسئلة التي سبق طرحها، ومعها الاستعلام الذي نتج عنها، محفوظة في Redis.
        عند تكرار السؤال يُعاد استخدام الاستعلام بدل كتابته من جديد — لكنه{" "}
        <strong>يُنفَّذ في كل مرة</strong>، فالأرقام في الإجابة هي أرقام اليوم
        دائمًا. تُفرَّغ الذاكرة تلقائيًا عند توثيق جدول أو تعديل المفاهيم.
      </p>

      {stats && !stats.available && (
        // Distinct from "no entries yet", which looks identical in the figures
        // and means something completely different. Worth saying plainly that
        // nothing is broken except the saving.
        <div className="cache-offline">
          لا يمكن الوصول إلى Redis، فذاكرة الأسئلة معطّلة مؤقتًا. النظام يجيب على
          كل الأسئلة كالمعتاد، لكن دون توفير في الوقت. شغّل الخدمة بالأمر{" "}
          <code dir="ltr">docker compose up -d redis</code>.
        </div>
      )}

      {stats?.available && (
        <div className="cache-stats">
          <div className="cache-stat">
            <span className="cache-stat-value">{Math.round(stats.hit_rate * 100)}٪</span>
            <span className="cache-stat-label">من الأسئلة أُعيد استخدامها</span>
          </div>
          <div className="cache-stat">
            <span className="cache-stat-value">{stats.hits}</span>
            <span className="cache-stat-label">مرة وُفِّر فيها توليد الاستعلام</span>
          </div>
          <div className="cache-stat">
            <span className="cache-stat-value">{stats.entries}</span>
            <span className="cache-stat-label">سؤالًا محفوظًا</span>
          </div>
          <div className="cache-stat">
            <span className="cache-stat-value">{asked}</span>
            <span className="cache-stat-label">سؤالًا في المجموع</span>
          </div>
        </div>
      )}

      {stats?.available && stats.cleared_at > 0 && (
        <p className="cache-note">آخر تفريغ للذاكرة: {formatTime(stats.cleared_at)}</p>
      )}

      <div className="cache-actions">
        <button type="button" className="activity-filter" onClick={load} disabled={busy}>
          تحديث
        </button>
        {entries.length > 0 &&
          (confirmingClear ? (
            <>
              <span className="catalog-hint">تفريغ الذاكرة بالكامل؟</span>
              <button
                type="button"
                className="activity-filter activity-clear-confirm"
                onClick={handleClear}
                disabled={busy}
              >
                {busy ? "جارٍ التفريغ…" : "نعم، فرّغها"}
              </button>
              <button
                type="button"
                className="activity-filter"
                onClick={() => setConfirmingClear(false)}
                disabled={busy}
              >
                إلغاء
              </button>
            </>
          ) : (
            <button
              type="button"
              className="activity-filter"
              onClick={() => setConfirmingClear(true)}
            >
              تفريغ الذاكرة
            </button>
          ))}
      </div>

      {error && <div className="error">{error}</div>}

      {entries.length === 0 && !error && (
        <p className="activity-empty">
          لا شيء بعد. اطرح سؤالًا من صفحة الأسئلة، ثم اطرحه مرة أخرى وسيظهر هنا.
        </p>
      )}

      <ul className="cache-list">
        {entries.map((entry) => (
          <li key={entry.key} className="cache-item">
            <div className="cache-item-head">
              <span className="cache-question" dir="auto">
                {entry.question}
              </span>
              <span className="cache-uses">
                {entry.uses > 0 ? `أُعيد استخدامه ${entry.uses} مرة` : "لم يتكرر بعد"}
              </span>
            </div>
            <pre className="cache-sql" dir="ltr">
              {entry.sql}
            </pre>
            <div className="cache-item-foot">
              <span dir="ltr">{(entry.table_names || []).join("، ") || "—"}</span>
              {entry.concept_terms?.length > 0 && (
                <span dir="auto">المصطلحات: {entry.concept_terms.join("، ")}</span>
              )}
              <span>حُفظ {formatTime(entry.created_at)}</span>
              <button
                type="button"
                className="activity-toggle"
                onClick={() => handleRemove(entry.key)}
                disabled={busy}
              >
                حذف
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
