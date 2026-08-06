import { useEffect, useMemo, useRef, useState } from "react";
import { clearActivity, fetchActivity, fetchCdcStatus, subscribeToActivity } from "./api";
import TableDocEditor from "./TableDocEditor";

/**
 * What the automated paths have been doing.
 *
 * Two sources feed this: a snapshot on mount, then a live SSE stream. The
 * snapshot matters because the interesting events happen when nobody is
 * watching — the whole reason this tab exists is that CDC and the Qdrant
 * watcher removed the person who used to see the result of clicking a button.
 */

const SOURCE_LABELS = {
  cdc: "Schema",
  concepts: "Concepts",
  qdrant_watch: "Qdrant",
  catalog: "Catalog",
};

const LEVEL_LABELS = {
  info: "",
  warning: "Needs attention",
  error: "Failed",
};

function formatTime(ts) {
  const date = new Date(ts * 1000);
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  return sameDay
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : date.toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}

/**
 * The parts of an event's details worth showing inline.
 *
 * Deliberately a whitelist rather than a dump of every key: details carry whole
 * documents and sample rows for the panels that need them, and rendering all of
 * that would bury the one line that says what happened.
 */
function detailLines(event) {
  const lines = [];
  const d = event.details || {};

  if (d.description) lines.push({ label: "Description written", value: d.description });
  if (Array.isArray(d.reasons) && d.reasons.length > 0) {
    lines.push({ label: "Reviewer said", value: d.reasons.join(" · ") });
  }
  if (d.suggested_description) {
    lines.push({ label: "Draft description", value: d.suggested_description });
  }
  if (Array.isArray(d.columns) && d.columns.length > 0) {
    // One row per column rather than a joined string. This is the per-column
    // text the model wrote and what retrieval matches questions against, so it
    // has to be scannable — run together into a paragraph it is unreadable, and
    // unreadable means nobody checks it.
    lines.push({ label: "Columns described", items: d.columns });
  }
  if (d.summary) lines.push({ label: "Change", value: d.summary });
  if (typeof d.row_count === "number" && d.row_count >= 0) {
    lines.push({ label: "Rows in table", value: String(d.row_count) });
  }

  for (const [key, label] of [
    ["upserted", "Sent to Qdrant"],
    ["deleted_from_qdrant", "Deleted from Qdrant"],
    ["added_to_file", "Added to concepts.json"],
    ["updated_in_file", "Updated in concepts.json"],
    ["removed_from_file", "Removed from concepts.json"],
    ["would_delete_from_qdrant", "Would have deleted from Qdrant"],
    ["would_remove_from_file", "Would have removed from concepts.json"],
  ]) {
    if (Array.isArray(d[key]) && d[key].length > 0) {
      lines.push({ label, value: d[key].join(", ") });
    }
  }

  return lines;
}

export default function ActivityPanel() {
  const [events, setEvents] = useState([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState("all");
  const [cdc, setCdc] = useState(null);
  const [expanded, setExpanded] = useState(() => new Set());
  // Which event's table is open for editing. One at a time: two editors on the
  // same table could each save over the other.
  const [editing, setEditing] = useState(null);
  // Two-step, because clearing cannot be undone. Not a browser confirm(): those
  // are easy to dismiss reflexively and impossible to style.
  const [confirmingClear, setConfirmingClear] = useState(false);
  const [clearing, setClearing] = useState(false);

  // Streamed events are prepended, so the ref keeps the ids already on screen
  // without making the effect depend on `events` and resubscribe on each one.
  const seenIds = useRef(new Set());

  useEffect(() => {
    let cancelled = false;

    fetchActivity(200)
      .then((data) => {
        if (cancelled) return;
        for (const event of data.events) seenIds.current.add(event.id);
        setEvents(data.events);
      })
      .catch((err) => !cancelled && setError(err.message));

    const unsubscribe = subscribeToActivity(
      (event) => {
        setConnected(true);
        if (seenIds.current.has(event.id)) return;
        seenIds.current.add(event.id);
        setEvents((prev) => [event, ...prev].slice(0, 500));
      },
      () => setConnected(false),
    );

    // EventSource fires no event on a successful open until data arrives, so
    // this marks the stream live rather than leaving it showing "reconnecting"
    // through a quiet period.
    const timer = setTimeout(() => !cancelled && setConnected(true), 500);

    return () => {
      cancelled = true;
      clearTimeout(timer);
      unsubscribe();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetchCdcStatus()
        .then((data) => !cancelled && setCdc(data))
        .catch(() => {});
    load();
    // Status is a counter, not an event — a slow poll is the right shape for it.
    const interval = setInterval(load, 15000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  const warningCount = useMemo(
    () => events.filter((event) => event.level !== "info").length,
    [events],
  );

  const visible = useMemo(() => {
    if (filter === "warnings") return events.filter((event) => event.level !== "info");
    if (filter === "all") return events;
    return events.filter((event) => event.source === filter);
  }, [events, filter]);

  async function handleClear() {
    setClearing(true);
    setError(null);
    try {
      await clearActivity();
      seenIds.current.clear();
      // Re-read rather than emptying locally: clearing leaves one event behind
      // saying it was cleared, and that should be on screen straight away.
      const data = await fetchActivity(200);
      for (const event of data.events) seenIds.current.add(event.id);
      setEvents(data.events);
      setExpanded(new Set());
      setEditing(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setClearing(false);
      setConfirmingClear(false);
    }
  }

  function toggle(id) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="activity">
      <p className="subtitle">
        Everything the system did on its own — tables documented from a schema change, concepts
        synced with Qdrant. Anything it wasn&apos;t sure about is flagged here rather than indexed.
        Use <strong>Edit descriptions</strong> on any table to rewrite what it wrote.
      </p>

      <div className="activity-status">
        <span className={`activity-dot${connected ? " activity-dot-live" : ""}`} />
        <span>{connected ? "Live" : "Reconnecting…"}</span>
        {cdc && (
          <>
            <span className="activity-status-sep">·</span>
            <span>
              Change capture {cdc.enabled && cdc.running ? "running" : "off"}
              {cdc.events_received > 0 && ` · ${cdc.events_received} events`}
            </span>
            {cdc.pending_tables.length > 0 && (
              <>
                <span className="activity-status-sep">·</span>
                <span>Waiting to document: {cdc.pending_tables.join(", ")}</span>
              </>
            )}
            {cdc.awaiting_review > 0 && (
              <>
                <span className="activity-status-sep">·</span>
                <span className="activity-status-warn">
                  {cdc.awaiting_review} table{cdc.awaiting_review === 1 ? "" : "s"} not indexed yet —
                  see Schema updates
                </span>
              </>
            )}
            {cdc.auto_documented > 0 && (
              <>
                <span className="activity-status-sep">·</span>
                <span>
                  {cdc.auto_documented} documented automatically, unread
                </span>
              </>
            )}
          </>
        )}
      </div>

      <div className="activity-filters">
        {[
          ["all", `All (${events.length})`],
          ["warnings", `Needs attention (${warningCount})`],
          ["cdc", "Schema"],
          ["concepts", "Concepts"],
          ["qdrant_watch", "Qdrant"],
        ].map(([id, label]) => (
          <button
            key={id}
            type="button"
            className={`activity-filter${filter === id ? " activity-filter-active" : ""}`}
            onClick={() => setFilter(id)}
          >
            {label}
          </button>
        ))}

        {events.length > 0 && (
          <div className="activity-clear">
            {confirmingClear ? (
              <>
                <span className="catalog-hint">Clear the whole log?</span>
                <button
                  type="button"
                  className="activity-filter activity-clear-confirm"
                  onClick={handleClear}
                  disabled={clearing}
                >
                  {clearing ? "Clearing…" : "Yes, clear it"}
                </button>
                <button
                  type="button"
                  className="activity-filter"
                  onClick={() => setConfirmingClear(false)}
                  disabled={clearing}
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                className="activity-filter"
                onClick={() => setConfirmingClear(true)}
              >
                Clear log
              </button>
            )}
          </div>
        )}
      </div>

      {error && <div className="error">{error}</div>}

      {visible.length === 0 && !error && (
        <p className="activity-empty">
          {events.length === 0
            ? "Nothing yet. Create a table in MySQL or edit data/concepts.json and it will show up here."
            : "Nothing matching this filter."}
        </p>
      )}

      <ul className="activity-list">
        {visible.map((event) => {
          const details = detailLines(event);
          const isOpen = expanded.has(event.id);
          // Only events about a specific table can be edited. Concept syncs and
          // watcher notices name no table and get no edit button.
          const tableName = event.details?.table_name;
          return (
            <li key={event.id} className={`activity-item activity-${event.level}`}>
              <div className="activity-head">
                <span className="activity-time">{formatTime(event.ts)}</span>
                <span className="activity-source">{SOURCE_LABELS[event.source] || event.source}</span>
                {event.level !== "info" && (
                  <span className={`activity-badge activity-badge-${event.level}`}>
                    {LEVEL_LABELS[event.level]}
                  </span>
                )}
              </div>
              <div className="activity-message">{event.message}</div>
              {(details.length > 0 || tableName) && (
                <>
                  <div className="activity-actions">
                    {details.length > 0 && (
                      <button
                        type="button"
                        className="activity-toggle"
                        onClick={() => toggle(event.id)}
                      >
                        {isOpen ? "Hide details" : "Details"}
                      </button>
                    )}
                    {tableName && (
                      // The way back to a description you disagree with. The
                      // review queue only holds an entry until someone clears
                      // it; after that a scan will not surface the table
                      // either, because it matches what is indexed.
                      <button
                        type="button"
                        className="activity-toggle"
                        onClick={() => setEditing(editing === event.id ? null : event.id)}
                      >
                        {editing === event.id ? "Cancel edit" : "Edit descriptions"}
                      </button>
                    )}
                  </div>
                  {isOpen && (
                    <dl className="activity-details">
                      {details.map(({ label, value, items }) => (
                        <div key={label} className="activity-detail">
                          <dt>{label}</dt>
                          <dd>
                            {items ? (
                              <ul className="activity-columns">
                                {items.map((column) => (
                                  <li key={column.name}>
                                    <code>{column.name}</code>
                                    <span
                                      className={
                                        column.description ? "" : "activity-column-empty"
                                      }
                                      dir={column.description ? "rtl" : "ltr"}
                                    >
                                      {column.description || "no description"}
                                    </span>
                                  </li>
                                ))}
                              </ul>
                            ) : (
                              value
                            )}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  )}
                  {editing === event.id && (
                    <TableDocEditor
                      tableName={tableName}
                      onClose={() => setEditing(null)}
                    />
                  )}
                </>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
