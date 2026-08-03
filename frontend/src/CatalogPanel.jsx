import { useState } from "react";
import { approveCatalog, scanCatalog } from "./api";

const SEVERITY_LABELS = {
  ok: "Looks fine",
  thin_description: "Description too vague",
  off_domain: "Doesn't belong here",
  unintelligible: "Not understandable",
};

const CHANGE_LABELS = {
  table_added: "New table",
  table_dropped: "Table removed from MySQL",
  table_modified: "Structure changed",
};

/** Dropped tables default to being deleted from the index; everything else to being synced. */
function defaultAction(change) {
  return change.change_type === "table_dropped" ? "delete" : "upsert";
}

/** A card with no description yet is the one thing still worth stopping for. */
function missingDescription(item) {
  return item.action === "upsert" && !item.doc.description.trim();
}

export default function CatalogPanel() {
  const [items, setItems] = useState(null); // null = never scanned
  const [scanning, setScanning] = useState(false);
  const [approving, setApproving] = useState(false);
  const [error, setError] = useState(null);
  const [summary, setSummary] = useState(null);
  const [approved, setApproved] = useState(null);

  function updateItem(tableName, patch) {
    setItems((prev) =>
      prev.map((item) =>
        item.table_name === tableName
          ? { ...item, ...(typeof patch === "function" ? patch(item) : patch) }
          : item,
      ),
    );
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
        Check whether MySQL has changed since the schema index was last built, and review anything
        new before it gets added.
      </p>

      <div className="catalog-actions">
        <button type="button" onClick={handleScan} disabled={scanning || approving}>
          {scanning ? "Checking MySQL…" : "Check for schema updates"}
        </button>
        {scanning && (
          <span className="catalog-hint">
            Reading the schema, sampling rows, and reviewing each change — this takes a moment.
          </span>
        )}
      </div>

      {error && <div className="error">{error}</div>}

      {approved && (
        <div className="catalog-approved">
          <strong>Synced to Qdrant.</strong>
          {approved.upserted.length > 0 && <div>Indexed: {approved.upserted.join(", ")}</div>}
          {approved.deleted.length > 0 && <div>Removed: {approved.deleted.join(", ")}</div>}
          {approved.skipped.length > 0 && <div>Left alone: {approved.skipped.join(", ")}</div>}
        </div>
      )}

      {summary && (
        <p className="catalog-summary">
          {summary.changed === 0
            ? `No changes. ${summary.live} tables in MySQL, ${summary.indexed} indexed — everything matches.`
            : `${summary.changed} change${summary.changed === 1 ? "" : "s"} found across ${summary.live} MySQL tables.`}
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
            {approving ? "Syncing…" : "Approve & sync to Qdrant"}
          </button>
          {blockedCount > 0 && (
            <span className="catalog-hint">
              {blockedCount} table{blockedCount === 1 ? "" : "s"} still need
              {blockedCount === 1 ? "s" : ""} a description.
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function ChangeCard({ item, onChange }) {
  const { verdict, doc } = item;
  // The scan's verdict is shown for information only — it never blocks a sync.
  const flagged = verdict.needs_edit;

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
          <strong>{item.table_name}</strong>
          <span className="change-type">{CHANGE_LABELS[item.change_type] ?? item.change_type}</span>
        </div>
        <span className={`badge badge-${flagged ? "warn" : "ok"}`}>
          {SEVERITY_LABELS[verdict.severity] ?? verdict.severity}
        </span>
      </div>

      <p className="change-summary">{item.summary}</p>

      {flagged && verdict.reasons.length > 0 && (
        <ul className="change-reasons">
          {verdict.reasons.map((reason, index) => (
            <li key={index}>{reason}</li>
          ))}
        </ul>
      )}

      {item.action !== "delete" && (
        <>
          <label className="field">
            <span>Description (this is what gets searched)</span>
            <textarea
              rows={3}
              value={doc.description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Describe what this table holds and how it relates to the others."
            />
          </label>

          {verdict.suggested_description && verdict.suggested_description !== doc.description && (
            <div className="suggestion">
              <span>Suggested: {verdict.suggested_description}</span>
              <button type="button" onClick={() => setDescription(verdict.suggested_description)}>
                Use this
              </button>
            </div>
          )}

          <details className="change-columns">
            <summary>Columns ({doc.columns.length})</summary>
            {doc.columns.map((column) => (
              <label key={column.name} className="field field-inline">
                <span>
                  {column.name} <em>{column.type}</em>
                </span>
                <input
                  type="text"
                  value={column.description}
                  onChange={(e) => setColumnDescription(column.name, e.target.value)}
                  placeholder="What does this column hold?"
                />
              </label>
            ))}
          </details>
        </>
      )}

      {item.sample_rows.length > 0 && (
        <details className="change-sample">
          <summary>
            Sample rows ({item.sample_rows.length} of {item.row_count})
          </summary>
          <pre>{JSON.stringify(item.sample_rows, null, 2)}</pre>
        </details>
      )}

      <div className="change-controls">
        <label>
          <input
            type="radio"
            checked={item.action !== "skip"}
            onChange={() => onChange({ action: defaultAction(item) })}
          />
          {item.change_type === "table_dropped" ? "Remove from index" : "Add to index"}
        </label>
        <label>
          <input
            type="radio"
            checked={item.action === "skip"}
            onChange={() => onChange({ action: "skip" })}
          />
          Leave alone
        </label>
      </div>
    </div>
  );
}
