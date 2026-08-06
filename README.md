# Text-to-SQL Retrieval System

Natural-language question → embed (BGE-M3) → semantic search over schema docs
(Qdrant) → retrieve relevant tables → LLM (Gemma 4 E4B) generates SQL →
FastAPI executes it read-only against MySQL → results go back to the LLM →
natural-language answer → React frontend.

```
User → React frontend → FastAPI /api/query/stream
                            ├─ embed question (BGE-M3 via LM Studio)
                            ├─ vector search Qdrant → relevant table docs
                            ├─ build schema prompt (tables + FK join hints)
                            ├─ Gemma 4 E4B (via LM Studio) → SQL
                            ├─ SQL guard (SELECT-only) → MySQL (read-only user)
                            └─ Gemma 4 E4B (via LM Studio) → natural-language answer
```

Each of those steps is reported to the browser as it happens, and both LLM
calls stream their tokens, so the UI shows which stage the run has reached and
renders the SQL and the final answer as they are written.

That is the read path. The other half keeps what it reads from going stale, so
that a table or a term added today is queryable today without anyone hand-editing
a Python file:

```
MySQL DDL ──► Debezium ──► /api/cdc/events ──► debounce (wait for rows)
                                                   │
                                          introspect + sample + LLM review
                                                   │
                                    ┌──────────────┴──────────────┐
                              clean verdict                 flagged
                                    │                           │
                            upsert to Qdrant            quarantine + warn
                              (queryable)              (Schema updates tab)

data/concepts.json ◄──────── three-way sync ────────► Qdrant legal_concepts
                        (snapshot + delete rail; Qdrant-side
                         deletions noticed via container logs)
```

Everything on this half reports to the **Activity** tab, since by definition
nobody is watching when it runs.

## Project layout

```
db/           MySQL schema + seed data + read-only user grant + CDC user grant
data/         concepts.json (the legal glossary) + runtime state (gitignored)
ingestion/    Schema doc builder + BGE-M3 embedding → Qdrant ingestion script
backend/      FastAPI service (retrieval, SQL generation, execution, response)
frontend/     Minimal React test harness (input box → answer)
docker-compose.yml   Qdrant for local dev, plus Debezium under the `cdc` profile
.env.example          Shared config template
```

## Prerequisites

- An existing MySQL instance (per spec — this repo only creates the schema/tables in it, it doesn't stand up MySQL itself)
- Docker (for Qdrant), or your own Qdrant endpoint
- [LM Studio](https://lmstudio.ai/) installed locally, with **Gemma 4 E4B** and a **BGE-M3** embedding model downloaded
- Python 3.11+
- Node.js 18+

## 1. Start Qdrant and LM Studio

```bash
docker compose up -d
```

In LM Studio: load Gemma 4 E4B and the BGE-M3 embedding model (Search tab →
download each), then go to **Developer > Start Server** (default
`http://localhost:1234`, OpenAI-compatible API).

Check `GET http://localhost:1234/v1/models` and confirm the exact model IDs
match `LLM_MODEL` / `EMBEDDING_MODEL` in your `.env` — LM Studio's IDs vary by
publisher/quantization, so the defaults in `.env.example` may need adjusting
to whatever you actually downloaded.

If you already run Qdrant elsewhere, skip `docker compose up -d` and point
`QDRANT_URL` at it instead.

## 2. Set up the database

Against your existing MySQL instance, as a user with admin/DDL rights:

```bash
mysql -h <host> -P <port> -u <admin_user> -p < db/01_schema.sql
mysql -h <host> -P <port> -u <admin_user> -p < db/02_seed.sql
```

Then create the read-only application user (edit the password in the file
first):

```bash
mysql -h <host> -P <port> -u <admin_user> -p < db/03_readonly_user.sql
```

The backend connects **only** as this read-only user. That grant — not the
application-level SQL guard — is what actually prevents writes; see
`backend/app/services/sql_guard.py` for the (defense-in-depth) app-level check.

## 3. Configure environment

```bash
cp .env.example .env
```

Fill in your MySQL host/credentials (the read-only user from step 2) and
adjust the Qdrant URL / LM Studio model IDs if not using the defaults.

## 4. Ingest schema docs into Qdrant

```bash
cd ingestion
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
python ingest.py
```

This builds one retrieval document per table (name, columns, types, foreign
keys, and a hand-written description — see `schema_docs.py`), embeds each
with BGE-M3, and upserts them into the `schema_docs` Qdrant collection.
Re-run any time you edit a description; it's idempotent (deterministic point
IDs per table).

## 5. Run the backend

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 --http h11 --ws none
```

Those last three flags matter if you are running the CDC path (section 8). Each
one fixes a specific failure that otherwise looks like Debezium being broken:

- **`--http h11`** — the default `httptools` parser stops reading the request
  body the moment it sees an `Upgrade:` header, and hands the app an empty one.
  Debezium's sink is a Java HTTP client, which always offers to upgrade to
  HTTP/2 (`Upgrade: h2c`). Every event therefore arrived with no body, was
  discarded as unparseable, and Debezium logged `Failed to publish event:
  Invalid HTTP request received` — the text of uvicorn's own 400 page, echoed
  back. `h11` parses the body regardless. Measured: 0 events accepted before,
  29 after, with nothing else changed.
- **`--host 0.0.0.0`** — the default binds `127.0.0.1` only, which a container
  cannot reach through `host.docker.internal`.
- **`--ws none`** — silences the "Unsupported upgrade request" warning that the
  same `Upgrade` header produces. Cosmetic; there are no WebSocket routes.

Endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness check |
| `POST /api/query` | Run a query, return the finished JSON result |
| `POST /api/query/stream` | Same pipeline, streamed as server-sent events |

Both take `{"question": "..."}`. The streaming endpoint emits one JSON object
per SSE `data:` frame:

| `type` | Payload | When |
| --- | --- | --- |
| `stages` | `stages: [{id, label}]` | Once, before the run starts |
| `stage` | `stage`, `status` (`started`/`completed`), `detail` | At each stage boundary |
| `token` | `stage`, `text` | Per LLM output chunk |
| `result` | `result: <QueryResponse>` | On success |
| `error` | `detail`, `status_code` | On failure |

Failures on the streaming endpoint arrive as an `error` event with HTTP 200,
not as an error status — the response headers are already sent by the time the
pipeline can fail.

Run tests:

```bash
pytest
```

## 6. Run the frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173, type a question (e.g. *"Which cases did Judge
Amara Okafor preside over?"* or *"What legal principles came out of the
Marshall v. Acme case?"*), and submit.

## 7. Keeping the index in sync (Schema updates tab)

Once MySQL changes — a new table, a new column, a dropped table — the indexed
schema docs go stale and the retriever starts answering from an outdated
picture of the database. The **Schema updates** tab handles that without
anyone hand-editing `ingestion/schema_docs.py`.

Press **Check for schema updates** and the backend:

1. reads the live structure from `information_schema`;
2. diffs it against the payloads already in Qdrant (which double as the "as of
   last ingestion" record — no timestamps or migration log needed);
3. for each changed table, samples a few real rows and asks the LLM whether a
   human needs to look: is the table intelligible, does it belong with the
   rest of the catalog, does its description actually match its data;
4. returns each change prefilled with a proposed description to edit.

Anything the reviewer flags is shown amber with its reasons, as information.
Edit the description freely — **whatever you submit is what gets indexed**. The
LLM's role ends at the scan: it exists to surface junk and to give you a draft
to start from, not to grade your text. Re-judging edits would make them
pointless, since a table flagged during the scan could never be cleared no
matter what was typed. **Approve & sync to Qdrant** only stops you if a table
has no description at all.

Notes:

- Run `python ingestion/ingest.py` once first, so there is a baseline to diff
  against. Otherwise the first scan reports the entire schema as new.
- Views are included alongside base tables, so a computed view like
  `case_congestion` is queryable by the assistant.
- If LM Studio is unreachable, tables come back flagged with an empty
  suggestion rather than silently passing — you still have to write the
  description yourself.
- Keep `CATALOG_REVIEW_CONCURRENCY=1` on LM Studio. Concurrent completions from
  one loaded model come back truncated, which makes every review fail closed
  with an empty description — a transport failure that looks exactly like a
  prompt bug.
- Skipping a table is a per-scan decision and is not remembered; an
  unresolved table will reappear on the next scan.

## 8. Documenting new tables automatically (Debezium)

Step 7 is the manual version: someone remembers to press a button. This is the
same thing without the someone — a table created in the SQL editor documents
itself and becomes queryable in the chatbot.

Debezium Server tails the MySQL binary log and POSTs each change event to
`/api/cdc/events`. The backend does *not* document straight from the event:
Debezium says a table was created, but the reviewer's strongest evidence is a
sample of real rows, and a `CREATE TABLE` arrives when the table is still empty.
So the event only marks the table as worth looking at, and the actual work runs
the exact same `introspect → diff → review` path the Scan button uses.

**Prerequisites.** Debezium needs more than the read-only query user has:

```sql
SELECT @@log_bin, @@binlog_format, @@binlog_row_image, @@server_id;
-- expected: 1, ROW, FULL, and any non-zero server id
```

If `log_bin` is 0, the binary log is off. That is set in `my.cnf`/`my.ini` and
needs a **server restart** — see the block at the bottom of `db/06_cdc_user.sql`.
Then create the replication user:

```bash
mysql -u root -p < db/06_cdc_user.sql   # edit the password in it first
```

**Start it.**

```bash
docker compose --profile cdc up -d
docker logs -f texttosql-debezium        # should reach "Connected to binlog at ..."
```

Make sure the backend was started with the flags from section 5 — in particular
`--http h11`. Without it Debezium connects, streams, and reports every event as
published, while the backend receives every one of them with an empty body and
silently discards it. Both sides look healthy and nothing works.

The profile is opt-in precisely because of those prerequisites: without them the
container crash-loops, so it stays out of the default `docker compose up`.

**What happens to a new table.**

1. `CREATE TABLE case_notes (...)` commits.
2. The event reaches the backend, which starts a debounce window
   (`CDC_DEBOUNCE_SECONDS`, default 20s). Any `INSERT`s that follow extend it,
   capped at `CDC_MAX_WAIT_SECONDS` — so a bulk load produces **one** review
   after it finishes, with rows to look at, rather than one per batch.
3. The window closes. The table is introspected, sampled, and reviewed.
4. **Clean verdict** → embedded and upserted into `schema_docs` unattended. It
   is queryable immediately.
   **Flagged verdict** → held in `data/quarantine.json`, *not* indexed, and
   raised as a warning in the Activity tab. It shows up in the Schema updates
   tab under the same review card, with a badge on the nav item.

That split is the one place the automation deliberately stops. A description the
model itself could not vouch for is not something to index silently, and the
rule from step 7 still holds: once a human writes the text, that text is what
gets indexed.

A table dropped in MySQL has its schema document deleted with no review — it
describes something that no longer exists, and leaving it indexed makes the
model write SQL against a missing table.

## 9. The legal glossary (Concepts tab)

`data/concepts.json` maps terms a judge would use onto the SQL that expresses
them — nothing in the database stores the string "محكمة الصلح", and no column
marks a case "متقادمة". Authoring rules are in `data/CONCEPTS.md`.

Edit the file directly, or upload a replacement in the **Concepts** tab. Either
way it reconciles against the `legal_concepts` Qdrant collection **both
directions**: delete a concept from the file and its point is removed; delete a
point in the Qdrant dashboard and it is removed from the file.

```bash
python ingestion/ingest_concepts.py --dry-run   # show the plan, touch nothing
python ingestion/ingest_concepts.py             # apply it
```

**How it knows which way.** Comparing two sides tells you they differ; it cannot
tell you which one moved. "Removed from the file" and "added to Qdrant" produce
an identical two-way diff. So the sync is three-way —
`data/.concepts-sync-state.json` records the state both sides last agreed on,
and each concept is judged against that rather than against the other side:

| file | snapshot | Qdrant | → |
|---|---|---|---|
| yes | no | no | added to the file → upsert into Qdrant |
| no | no | yes | added to Qdrant → add to the file |
| yes | yes | no | deleted from Qdrant → remove from the file |
| no | yes | yes | deleted from the file → delete from Qdrant |
| edited | yes | same | edited in the file → upsert |
| same | yes | edited | edited in Qdrant → write back to the file |
| edited | yes | edited | edited in both → the file wins, warn |

Do not edit or delete the snapshot. Deleting it is not destructive — with no
record of an agreement everything looks newly added, so the next sync *merges*
rather than deletes — but you lose the ability to distinguish a deletion until
the next successful sync.

**The delete rail.** A sync that would remove more than
`CONCEPT_SYNC_DELETE_RATIO` (default 30%) of either side refuses and raises a
warning instead. This is not theoretical: if Qdrant ever restarts on an empty
volume, every concept looks deleted and an unguarded reconcile would empty
`concepts.json` to match. Force it with `--force` (or the button in the tab)
once you have looked at what it wanted to do.

**Noticing outside deletions.** Qdrant has no change feed, so the backend tails
its container log. Worth knowing what that log actually contains — measured on
qdrant:v1.9.4:

```
INFO ...collection_meta_ops: Deleting collection legal_concepts
INFO actix_web...logger: "POST /collections/legal_concepts/points/delete" 200
```

A collection drop is named. A point delete is an access-log line with **no
request body**, so the deleted ids are not recoverable from it. That is why the
watcher only ever triggers a full reconcile and never reads identity out of the
log. Blind spot: only REST (6333) is logged, so a delete issued over gRPC (6334)
produces no line at all — `qdrant-client` defaults to REST, so the app's own
writes are covered.

## 10. The Activity tab

Everything above happens when nobody is watching, which is the problem this tab
solves. It streams (SSE) every automated action: tables documented, concepts
synced, syncs refused, reviews that came back flagged. Warnings and errors stay
visible and filterable; the backing store is an append-only JSONL at
`data/activity.jsonl`, so a backend restart does not lose the history — which
matters, because restarting the backend is the first thing anyone does when
something has gone wrong.

## Design notes

- **Language (Arabic)**: the system is Arabic-facing. Schema descriptions and
  `data/concepts.json` are Arabic, and the answer and catalog-review
  prompts produce Arabic. Identifiers — table names, column names, foreign
  keys — stay English on purpose: the user never sees them, the LLM writes
  measurably better SQL against them, and it avoids backtick-quoting every
  identifier in generated queries. The SQL-generation prompt itself is also
  English, since instruction-following on code generation is more reliable
  that way; only the rules describing Arabic input/output are localized.
  ENUM and category columns still store English values (`'Open'`,
  `'Contract Law'`), so each such column's description glosses them in Arabic
  — that mapping is what turns "القضايا المفتوحة" into `status = 'Open'`.
  When the underlying data is migrated to Arabic, update those glosses.

- **Schema retrieval granularity**: every ingested document is table-level
  (`level: "table"` in the Qdrant payload). Column metadata is already fully
  structured per table (`ingestion/schema_docs.py`'s `ColumnDoc`), so
  column-level documents can be added later as `level: "column"` points
  without touching the table-level path — `retrieval.py` already filters on
  `level` explicitly for this reason.
- **Dense-only embeddings for now**: BGE-M3 supports dense/sparse/multi-vector
  output, but LM Studio's OpenAI-compatible `/v1/embeddings` endpoint only
  exposes the dense vector. The ingestion and retrieval paths only depend on
  "a list of floats" from the embedding client, so swapping in a server that
  exposes sparse weights later is isolated to `embeddings.py` in each service.
- **Why LM Studio**: it runs Gemma 4 E4B and BGE-M3 fully locally behind an
  OpenAI-compatible API, so no external API keys or network calls are needed
  for either model. Both `llm.py` and `embeddings.py` only assume an
  OpenAI-style `/chat/completions` / `/embeddings` endpoint, so pointing
  `LM_STUDIO_BASE_URL` at a different OpenAI-compatible server (vLLM, another
  Ollama-with-OpenAI-shim, a hosted endpoint) would work without code changes.
- **Read-only is enforced twice**: the MySQL user has `SELECT`-only grants
  (the real guardrail), and `sql_guard.py` independently rejects anything
  that isn't a single `SELECT` statement (defense in depth, fails fast with a
  clear error instead of relying on the DB to reject it). A `LIMIT` is
  auto-appended if the LLM omits one.
- **Joins**: when retrieval returns multiple tables, their foreign keys are
  rendered as explicit "Relationships" hints in the LLM prompt so generated
  SQL uses proper `JOIN`s instead of guessing column names.
- **Two LLM calls per query**: one to generate SQL from the retrieved schema
  context, one to turn the executed query's results into a natural-language
  answer — matching the spec's "results returned to the LLM for a final
  natural-language response."
- **Conversation memory lives in the browser**: the backend keeps no session
  state — the chat page replays the transcript with every question, exactly as
  the catalog flow keeps its run state client-side. What is replayed is the
  question, the SQL and the answer *text* of each past turn, never the result
  rows: one 200-row result outweighs the schema, the system prompt and the rest
  of the conversation combined. Past turns enter the SQL prompt as real
  `user`/`assistant` message pairs, so a follow-up like "بس المفتوحة منها" is
  the model editing its own last query rather than writing a new one blind.
- **Retrieval carries the previous turn's tables forward**: a follow-up names
  no table of its own, so embedding it alone retrieves past the subject the
  conversation is actually about — and takes the prior SQL in the prompt out of
  context with it. The tables the last question resolved to are appended to
  this question's top-k, deduplicated and capped by `CONTEXT_CARRY_TABLES`.
  This is cheaper and more honest than an LLM call that rewrites the question,
  which can silently rewrite it wrong.
- **The context window is enforced here, not by LM Studio**: every request is
  costed against `CONTEXT_WINDOW_TOKENS` before any LLM call, and a full
  conversation is refused with HTTP 413 so the user starts a new chat. Turns
  are never silently dropped. Both halves of that matter — overflow inside LM
  Studio truncates the *start* of the prompt, which is the schema, so the model
  would answer with invented columns rather than admit it lost the thread; and
  an assistant that quietly forgets which case you were discussing is worse
  than one that stops. Counts are deliberately over-estimates (see
  `services/context.py`), because being wrong low is the dangerous direction.
- **Off-topic questions are refused at SQL generation, not before it**: vector
  search always returns its top-k tables, so retrieval cannot tell "off-domain"
  from "hard" on its own. The obvious fix — an LLM relevance gate in front of
  the embedding step — was tried and removed: judging on table descriptions
  alone, it read questions too literally and rejected real ones, because a
  category the user names in their own words ("القضايا البسيطة") lives in a
  *column* value ("مخالفات وجنح بسيطة") that the gate never sees. SQL generation
  is the first step holding the full column list and their permitted values, so
  it is the first step that can tell the two apart. It answers `NO_QUERY:
  <سبب عربي>` and the pipeline shows that reason to the user verbatim.
- **One pipeline, two endpoints**: `services/pipeline.py` is a single async
  generator that yields stage events, LLM token chunks, and finally the
  result. `/api/query/stream` forwards those events; `/api/query` discards
  everything but the result. Neither endpoint can drift from the other,
  because there is only one implementation of the pipeline.
- **The frontend doesn't know the stage list**: the server sends its ordered
  `STAGES` definition as the first event of the stream, so adding or renaming
  a pipeline step is a backend-only change.
- **SSE over a POST body**: `EventSource` can only issue GETs, so
  `frontend/src/api.js` reads the event stream off the `fetch` response body
  itself. Question text stays in the request body rather than a query string.

## Extending later (deliberately out of scope for this pass)

- Column-level retrieval (`ingestion/ingest.py::build_column_documents` is a
  stubbed entry point)
- Column **value profiles**: distinct values for low-cardinality VARCHAR
  columns (`case_type`, `verdict`, `hearing_type`, `area_of_law`) folded into
  the table payload, so the LLM filters on real literals instead of inventing
  them. ENUM columns already expose their values through the type string, so
  this is only worth doing for the non-ENUM ones — which is where the
  `COUNT(DISTINCT ...)` cost and a PII skip-list come in.
- Hybrid dense+sparse Qdrant search
- Query result caching, auth, multi-turn conversation
- Persisting conversations across a page reload (the transcript lives in
  component state, so a refresh starts a new chat)
