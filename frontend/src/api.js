const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

/** POST JSON and unwrap FastAPI's `detail` on failure. */
async function postJson(path, body) {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const data = await response.json().catch(() => null);

  if (!response.ok) {
    const error = new Error(
      // `detail` is a string for ordinary errors, but the approval gate returns
      // a structured object listing which tables it blocked.
      typeof data?.detail === "string"
        ? data.detail
        : data?.detail?.message || `فشل الطلب (${response.status})`,
    );
    error.status = response.status;
    error.detail = data?.detail;
    throw error;
  }

  return data;
}

/** Compare live MySQL against the indexed schema docs. Returns reviewed changes. */
export function scanCatalog() {
  return postJson("/api/catalog/scan", {});
}

/** Sync the reviewed changes to Qdrant exactly as submitted. */
export function approveCatalog(items) {
  return postJson("/api/catalog/approve", { items });
}

/** GET JSON, unwrapping FastAPI's `detail` on failure. */
async function getJson(path) {
  const response = await fetch(`${API_BASE_URL}${path}`);
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(data?.detail || `فشل الطلب (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

/**
 * Everything the automated path documented.
 *
 * Two kinds come back, distinguished by `indexed`: flagged tables that were NOT
 * written to Qdrant and cannot be queried yet, and tables that were indexed
 * automatically and work already but whose description nobody has read.
 */
export function fetchReviewQueue() {
  return getJson("/api/catalog/review-queue");
}

/**
 * The document currently indexed for one table, for editing.
 *
 * One table by name, not a catalog listing — it backs the edit button on an
 * activity event, where you are already looking at what the model wrote. Once a
 * review-queue entry has been cleared this is the only way back to it, because
 * a scan only reports tables that differ from the index.
 */
export function fetchTableDoc(tableName) {
  return getJson(`/api/catalog/tables/${encodeURIComponent(tableName)}`);
}

/** Drop an entry without changing what is indexed. */
export async function dismissReview(tableName) {
  const response = await fetch(
    `${API_BASE_URL}/api/catalog/review-queue/${encodeURIComponent(tableName)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.detail || `فشل الطلب (${response.status})`);
  }
  return response.json();
}

/** What the CDC worker is doing, plus how many tables are waiting for review. */
export function fetchCdcStatus() {
  return getJson("/api/cdc/status");
}

/**
 * The glossary as Qdrant holds it — what the query pipeline actually reads.
 *
 * This is what the admin page lists and edits. The file is a backup written
 * after the fact, so showing it would risk showing a definition that is not in
 * force yet.
 */
export function fetchLiveConcepts() {
  return getJson("/api/concepts/live");
}

/**
 * Create or overwrite one concept in Qdrant, keyed by its id.
 *
 * Both the edit form and "new concept" go through here: from Qdrant's side they
 * are the same upsert, and the id in the body is the only thing that decides
 * which of the two it turns out to be.
 */
export async function saveConcept(concept) {
  const response = await fetch(`${API_BASE_URL}/api/concepts/entry`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(concept),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(
      typeof data?.detail === "string"
        ? data.detail
        : data?.detail?.[0]?.msg || `فشل الطلب (${response.status})`,
    );
  }
  return data;
}

/* The file-side endpoints — GET /api/concepts, POST /api/concepts/sync and the
   wholesale PUT — are still served by the backend and still used by the Qdrant
   watcher, but nothing in the UI calls them any more: the admin page writes to
   Qdrant directly and the file follows. */

/** The most recent activity events, newest first. */
export function fetchActivity(limit = 100) {
  return getJson(`/api/activity?limit=${limit}`);
}

/**
 * Empty the activity log.
 *
 * Only the record of what happened — the review queue, the schema index and
 * everything else are untouched.
 */
export async function clearActivity() {
  const response = await fetch(`${API_BASE_URL}/api/activity`, { method: "DELETE" });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.detail || `فشل الطلب (${response.status})`);
  }
  return response.json();
}

/**
 * Subscribe to the live activity stream.
 *
 * A plain EventSource is enough here — unlike the query stream this is a GET
 * with no body, so none of submitQueryStream's manual fetch/parse machinery is
 * needed. The browser also reconnects on its own if the backend restarts.
 *
 * @returns {() => void} an unsubscribe function
 */
export function subscribeToActivity(onEvent, onError) {
  const source = new EventSource(`${API_BASE_URL}/api/activity/stream`);

  source.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data));
    } catch {
      // A frame we can't parse is not worth tearing the stream down for.
    }
  };
  source.onerror = () => onError?.();

  return () => source.close();
}

/**
 * Run a query against the streaming endpoint, reporting progress as it happens.
 *
 * EventSource can't issue a POST, so this reads the server-sent event stream
 * off the fetch response body directly.
 *
 * The backend holds no session state, so `history` — the transcript so far — is
 * replayed with every question. Errors carry the server's status code so the
 * caller can tell a full context window (413) from an ordinary failure.
 *
 * Aborting `signal` drops the connection, which closes the server's event
 * generator and cancels the LM Studio request behind it — so stopping actually
 * frees the model rather than just hiding its output. The abort surfaces as an
 * `AbortError`, which callers should treat as a cancellation, not a failure.
 *
 * @param {string} question
 * @param {Array<{question: string, sql: string, answer: string, table_names: string[]}>} history
 * @param {{ onStages?, onStage?, onToken?, onUsage?, signal?: AbortSignal }} handlers
 * @returns {Promise<object>} the final QueryResponse
 */
export async function submitQueryStream(question, history = [], handlers = {}) {
  const { onStages, onStage, onToken, onUsage, signal } = handlers;

  const response = await fetch(`${API_BASE_URL}/api/query/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ question, history }),
    signal,
  });

  if (!response.ok || !response.body) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.detail || `فشل الطلب (${response.status})`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;

  const handleEvent = (event) => {
    switch (event.type) {
      case "stages":
        onStages?.(event.stages);
        break;
      case "stage":
        onStage?.(event);
        break;
      case "token":
        onToken?.(event);
        break;
      case "usage":
        onUsage?.(event.usage);
        break;
      case "result":
        result = event.result;
        break;
      case "error": {
        const error = new Error(event.detail);
        error.status = event.status_code;
        throw error;
      }
      default:
        break;
    }
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line; the trailing piece may be partial.
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";

      for (const frame of frames) {
        const data = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim())
          .join("");
        if (data) handleEvent(JSON.parse(data));
      }
    }
  } finally {
    reader.cancel().catch(() => {});
  }

  if (!result) {
    // An abort lands here only if it raced the last read; report it as the
    // cancellation it is rather than as a dropped connection.
    if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
    throw new Error("انقطع الاتصال قبل وصول الإجابة.");
  }
  return result;
}
