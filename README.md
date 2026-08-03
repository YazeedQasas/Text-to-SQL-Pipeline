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

## Project layout

```
db/           MySQL schema + seed data + read-only user grant
ingestion/    Schema doc builder + BGE-M3 embedding → Qdrant ingestion script
backend/      FastAPI service (retrieval, SQL generation, execution, response)
frontend/     Minimal React test harness (input box → answer)
docker-compose.yml   Qdrant for local dev
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
uvicorn app.main:app --reload --port 8000
```

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

## Design notes

- **Language (Arabic)**: the system is Arabic-facing. Descriptions in
  `ingestion/schema_docs.py` are Arabic, and the answer and catalog-review
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
- Cancelling an in-flight run from the UI (the SSE reader is cancelled on
  unmount, but the server-side pipeline runs to completion)
