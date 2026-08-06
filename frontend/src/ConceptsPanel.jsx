import { useEffect, useRef, useState } from "react";
import { fetchConcepts, syncConcepts, uploadConcepts } from "./api";

/**
 * The legal glossary: view it, replace it by uploading a JSON file, sync it.
 *
 * The sync itself is three-way and lives on the backend — this panel never
 * decides what to add or delete, it only triggers a reconcile and reports what
 * came back. That matters for the refusal case: when the sync declines to
 * delete a large share of either side, the answer is a normal 200 carrying
 * `ok: false`, and the only thing the UI does about it is show the reason and
 * offer to run it again with force.
 */

function summarise(result) {
  const parts = [];
  const rows = [
    ["upserted", "sent to Qdrant"],
    ["deleted_from_qdrant", "deleted from Qdrant"],
    ["added_to_file", "added to concepts.json"],
    ["updated_in_file", "updated in concepts.json"],
    ["removed_from_file", "removed from concepts.json"],
  ];
  for (const [key, label] of rows) {
    if (result[key]?.length > 0) parts.push(`${result[key].length} ${label}`);
  }
  return parts.length > 0 ? parts.join(", ") : "already in sync — nothing to do";
}

export default function ConceptsPanel() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [query, setQuery] = useState("");
  const fileInput = useRef(null);

  async function load() {
    try {
      setData(await fetchConcepts());
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function runSync(force = false) {
    setBusy(true);
    setError(null);
    try {
      const outcome = await syncConcepts({ force });
      setResult(outcome);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;

    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      const concepts = Array.isArray(parsed) ? parsed : parsed.concepts;
      if (!Array.isArray(concepts)) {
        throw new Error('Expected a JSON array, or an object with a "concepts" array.');
      }

      const outcome = await uploadConcepts(concepts);
      setResult(outcome);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
      // Reset so re-picking the same file after a fix still fires a change event.
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  function download() {
    const body = JSON.stringify({ concepts: data.concepts }, null, 2);
    const url = URL.createObjectURL(new Blob([body], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "concepts.json";
    link.click();
    URL.revokeObjectURL(url);
  }

  const concepts = data?.concepts ?? [];
  const needle = query.trim().toLowerCase();
  const visible = needle
    ? concepts.filter((concept) =>
        [concept.id, concept.term, concept.definition, ...(concept.aliases || [])]
          .join(" ")
          .toLowerCase()
          .includes(needle),
      )
    : concepts;

  return (
    <div className="concepts">
      <p className="subtitle">
        Terms a judge would use that the database does not store — each one mapped to the SQL that
        expresses it. Edit <code>data/concepts.json</code> directly, or upload a replacement here.
      </p>

      <div className="catalog-actions">
        <button type="button" onClick={() => fileInput.current?.click()} disabled={busy}>
          Upload concepts.json
        </button>
        <button type="button" onClick={() => runSync(false)} disabled={busy}>
          {busy ? "Syncing…" : "Sync with Qdrant"}
        </button>
        <button type="button" onClick={download} disabled={busy || concepts.length === 0}>
          Download current
        </button>
        <input
          ref={fileInput}
          type="file"
          accept="application/json,.json"
          onChange={handleFile}
          hidden
        />
      </div>

      {data && (
        <p className="catalog-summary">
          {concepts.length} in concepts.json · {data.qdrant_count} in Qdrant
          {data.in_sync ? "" : " — out of step, run a sync"}
        </p>
      )}

      {error && <div className="error">{error}</div>}

      {result && !result.ok && (
        <div className="concepts-refused">
          <strong>Sync refused.</strong>
          <div>{result.refused_reason}</div>
          {/* An upload has already written the file, so forcing only re-runs
              the reconcile — there is nothing to re-send either way. */}
          <button type="button" onClick={() => runSync(true)} disabled={busy}>
            I checked — do it anyway
          </button>
        </div>
      )}

      {result?.ok && (
        <div className="catalog-approved">
          <strong>Synced.</strong> <span>{summarise(result)}</span>
          {result.conflicts.length > 0 && (
            <div className="concepts-conflicts">
              {result.conflicts.length} concept{result.conflicts.length === 1 ? " was" : "s were"}{" "}
              edited in both places; the concepts.json version was kept:{" "}
              {result.conflicts.map((conflict) => conflict.concept_id).join(", ")}
            </div>
          )}
        </div>
      )}

      {concepts.length > 0 && (
        <input
          type="search"
          className="concepts-search"
          placeholder="Filter concepts…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      )}

      <ul className="concepts-list">
        {visible.map((concept) => (
          <li key={concept.id} className="concept-card">
            <div className="concept-head">
              <span className="concept-term" dir="rtl">
                {concept.term}
              </span>
              <code className="concept-id">{concept.id}</code>
            </div>
            {concept.aliases?.length > 0 && (
              <div className="concept-aliases" dir="rtl">
                {concept.aliases.join("، ")}
              </div>
            )}
            <p className="concept-definition" dir="rtl">
              {concept.definition}
            </p>
            <code className="concept-sql">{concept.sql}</code>
            {concept.tables?.length > 0 && (
              <div className="concept-tables">{concept.tables.join(" · ")}</div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
