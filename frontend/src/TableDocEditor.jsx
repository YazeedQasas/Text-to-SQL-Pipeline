import { useEffect, useState } from "react";
import { approveCatalog, fetchTableDoc } from "./api";

/**
 * Edit the descriptions currently indexed for one table, and overwrite them.
 *
 * Opens from an activity event, where you have just read what the model wrote
 * and disagree with it. It loads the document as Qdrant holds it *now* rather
 * than reusing the text carried on the event — the event is a record of one
 * moment, and the table may have been re-documented or hand-edited since.
 * Saving stale text over newer text would be a silent regression.
 *
 * Saving goes through the same /approve endpoint the review flow uses, which
 * writes what it is given verbatim. Nothing re-judges the text on the way in:
 * a human who just read the sample rows knows more than a local 4B model does.
 */
export default function TableDocEditor({ tableName, onSaved, onClose }) {
  const [doc, setDoc] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchTableDoc(tableName)
      .then((data) => !cancelled && setDoc(data))
      .catch((err) => !cancelled && setError(err.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [tableName]);

  function setDescription(description) {
    setDoc((prev) => ({ ...prev, description }));
    setSaved(false);
  }

  function setColumnDescription(name, description) {
    setDoc((prev) => ({
      ...prev,
      columns: prev.columns.map((column) =>
        column.name === name ? { ...column, description } : column,
      ),
    }));
    setSaved(false);
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      await approveCatalog([{ doc, action: "upsert" }]);
      setSaved(true);
      onSaved?.(tableName);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  if (loading) return <div className="doc-editor doc-editor-loading">Loading…</div>;

  if (error && !doc) {
    return (
      <div className="doc-editor">
        <div className="error">{error}</div>
        <button type="button" className="dismiss" onClick={onClose}>
          Close
        </button>
      </div>
    );
  }

  if (!doc) return null;

  const emptyColumns = doc.columns.filter((column) => !column.description.trim()).length;

  return (
    <div className="doc-editor">
      <p className="catalog-hint">
        This is what is indexed for <strong>{tableName}</strong> right now. Whatever you save
        replaces it — the descriptions are what questions get matched against, so this is what
        decides whether the chatbot can find this table.
      </p>

      <label className="field">
        <span>Table description</span>
        <textarea
          rows={4}
          value={doc.description}
          onChange={(e) => setDescription(e.target.value)}
          dir="rtl"
          placeholder="Describe what this table holds and how it relates to the others."
        />
      </label>

      <div className="doc-editor-columns">
        <div className="doc-editor-columns-head">
          Column descriptions ({doc.columns.length})
          {emptyColumns > 0 && ` — ${emptyColumns} empty`}
        </div>
        {doc.columns.map((column) => (
          <label key={column.name} className="field field-inline">
            <span>
              {column.name} <em>{column.type}</em>
            </span>
            <input
              type="text"
              value={column.description}
              onChange={(e) => setColumnDescription(column.name, e.target.value)}
              dir="rtl"
              placeholder="What does this column hold?"
            />
          </label>
        ))}
      </div>

      {error && <div className="error">{error}</div>}

      <div className="doc-editor-footer">
        <button
          type="button"
          className="approve"
          onClick={handleSave}
          disabled={saving || !doc.description.trim()}
        >
          {saving ? "Saving…" : "Overwrite in Qdrant"}
        </button>
        <button type="button" className="dismiss" onClick={onClose}>
          Close
        </button>
        {!doc.description.trim() && (
          <span className="catalog-hint">
            A table with no description can never be found by a question.
          </span>
        )}
        {saved && <span className="doc-editor-saved">Saved — re-embedded and overwritten.</span>}
      </div>
    </div>
  );
}
