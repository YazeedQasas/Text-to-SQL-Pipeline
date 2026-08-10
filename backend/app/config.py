import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Repo root, resolved from this file (backend/app/config.py) rather than the
# process working directory — uvicorn is started from backend/ but the shared
# data/ directory sits one level above, and a relative default would break
# depending on where the server happened to be launched from.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _path_from_env(name: str, default_relative: str) -> Path:
    """Resolve a configurable path, anchoring relative values at the repo root."""
    raw = os.getenv(name)
    if not raw:
        return REPO_ROOT / default_relative
    path = Path(raw).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path

# --- MySQL (read-only user; see db/03_readonly_user.sql) -------------------
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "texttosql_ro")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "legal_db")

# --- Qdrant ------------------------------------------------------------------
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "schema_docs")
RETRIEVAL_TOP_K = int(os.getenv("RETRIEVAL_TOP_K", "5"))

# --- Domain glossary (data/concepts.json) ------------------------------------
# Palestinian legal vocabulary that has no counterpart in the schema: nothing
# stores "محكمة الصلح", and no column marks a case "مدورة". Each point maps such
# a term to the SQL fragment that expresses it.
QDRANT_CONCEPTS_COLLECTION = os.getenv("QDRANT_CONCEPTS_COLLECTION", "legal_concepts")
CONCEPT_TOP_K = int(os.getenv("CONCEPT_TOP_K", "3"))
# Minimum cosine score for a concept to be used at all. Unlike schema retrieval
# — which must always return tables — this search is expected to return NOTHING
# most turns, because most questions contain no special term. The threshold is
# what makes abstaining the default.
#
# Applied to the best-scoring FRAGMENT of the question, not the whole question
# (see services/arabic.py). Scoring whole questions put the same concept at
# 0.6157 on a short question and 0.5046 on a long one, which left no usable
# threshold; per-fragment scoring removes that dilution.
#
# CALIBRATED. Over a 15-question probe the vector signal caught 6 of 8 true
# matches at 0.60 with zero false positives (highest false: 0.5678, from
# "للطرف المدعي" in a question about email addresses). The two it misses are
# caught by the lexical signal, which needs no threshold. Re-measure both
# whenever concepts are added.
CONCEPT_SCORE_THRESHOLD = float(os.getenv("CONCEPT_SCORE_THRESHOLD", "0.60"))
# How long the in-memory copy of the glossary lives. Lexical matching tests
# every concept's surface forms against the question, so the whole (small)
# collection is held in memory rather than searched. The TTL is what makes a
# concept sync take effect without restarting the backend. The sync also busts
# this cache directly, so the TTL only matters for edits made some other way.
CONCEPT_CACHE_TTL_SECONDS = float(os.getenv("CONCEPT_CACHE_TTL_SECONDS", "300"))
# How many of the RETRIEVAL_TOP_K schema slots matched concepts may claim.
# A concept's SQL fragment references its tables by name, so those tables must
# reach the prompt or the fragment is unusable — but three concepts naming three
# tables each would fill every slot and evict the tables the question itself
# matched. This reserves the remainder for vector hits.
CONCEPT_BOOST_TABLES = int(os.getenv("CONCEPT_BOOST_TABLES", "3"))

# --- LM Studio (LLM + embeddings, OpenAI-compatible local server) -----------
# Enable via LM Studio > Developer > Start Server (default port 1234), with
# both the Gemma 4 E4B and BGE-M3 models loaded. Model identifiers below must
# match exactly what LM Studio reports at GET /v1/models.
LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-bge-m3")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemma-4-e4b")

# --- Schema catalog review ----------------------------------------------------
# How many real rows to show the reviewing LLM per changed table. This is the
# strongest signal it has for telling a legitimately-named domain table apart
# from a scratch import, so it is worth more than a couple of rows — but it all
# goes into the prompt, so keep it small.
CATALOG_SAMPLE_ROWS = int(os.getenv("CATALOG_SAMPLE_ROWS", "5"))
# Must stay at 1 against LM Studio. Serving concurrent completions from a single
# loaded model truncates responses: the stream ends mid-object and json.loads
# fails with "Unterminated JSON object". Reviews then fall back to the
# fail-closed verdict, so every new table comes back flagged with an empty
# description — which looks like a prompt bug but is a transport one. Raise this
# only against a backend that genuinely supports parallel inference.
CATALOG_REVIEW_CONCURRENCY = int(os.getenv("CATALOG_REVIEW_CONCURRENCY", "1"))

# --- Conversation context -----------------------------------------------------
# The browser holds the transcript and replays it with every question; the
# backend stays stateless. These bound how much of it the model is asked to
# carry.
#
# CONTEXT_WINDOW_TOKENS must match the context length the model is actually
# loaded with in LM Studio (Developer > loaded model > Context Length), NOT the
# model's maximum. Setting it higher than the loaded value moves the failure
# from a clean "start a new chat" into silent truncation, which drops the START
# of the prompt — the schema — and leaves the model inventing column names.
CONTEXT_WINDOW_TOKENS = int(os.getenv("CONTEXT_WINDOW_TOKENS", "16000"))
# Head-room kept free for the model's own reply.
CONTEXT_OUTPUT_RESERVE_TOKENS = int(os.getenv("CONTEXT_OUTPUT_RESERVE_TOKENS", "800"))
# An abuse rail, NOT the memory limit. The token budget is what decides when a
# conversation is full, because that is the limit the user is shown and the one
# that produces a clean stop. This cap only stops a client replaying an absurd
# transcript from being expensive to reject.
#
# It must stay ABOVE the number of turns the token budget allows, which scales
# with CONTEXT_WINDOW_TOKENS — roughly (window - schema - system) / 80 for this
# schema, so ~560 turns at a 50k window. Set it below that and it quietly drops
# the oldest turns before the budget can stop cleanly, which is exactly the
# behaviour this design exists to avoid.
CONTEXT_MAX_TURNS = int(os.getenv("CONTEXT_MAX_TURNS", "1000"))
# Extra schema tables carried over from the previous turn, on top of
# RETRIEVAL_TOP_K. A follow-up ("and who was the judge?") retrieves nothing
# useful on its own, so the tables the previous question resolved to are kept
# in the prompt — otherwise the prior SQL in the history references tables
# whose columns are no longer visible.
CONTEXT_CARRY_TABLES = int(os.getenv("CONTEXT_CARRY_TABLES", "3"))

# --- Query execution safety ---------------------------------------------------
MAX_RESULT_ROWS = int(os.getenv("MAX_RESULT_ROWS", "200"))
SQL_STATEMENT_TIMEOUT_SECONDS = int(os.getenv("SQL_STATEMENT_TIMEOUT_SECONDS", "10"))

# --- Repeated-question cache (Redis) -------------------------------------------
# Stores the SQL a question resolved to, keyed by the question's normalized form,
# so a repeat skips both the embedding call (~2.7s, measured) and the SQL
# generation completion. The stored SQL is re-executed every time and the answer
# is regenerated from the fresh rows — result data is never cached.
#
# Matching is LEXICAL only. See services/query_cache.py for why a semantic layer
# is not here yet: it cannot skip the embedding, and questions that differ by a
# year, a place name or a negation embed almost identically while needing
# entirely different SQL.
#
# Redis is not load-bearing. Every operation fails soft: if the server is down,
# every question is a cache miss and the pipeline runs exactly as it did before
# this feature existed. Nothing here is worth failing a good answer over.
QUERY_CACHE_ENABLED = os.getenv("QUERY_CACHE_ENABLED", "true").lower() == "true"
# Whether a question asked partway through a conversation may be answered from
# the cache. Entries are ALWAYS written from first turns only, so everything in
# the cache is context-free by construction — it got there from someone asking
# cold, with no conversation to lean on. Reading on later turns is what makes
# the feature useful at all: real sessions are one long chat containing many
# unrelated self-contained questions, and requiring a fresh chat per hit means
# the cache almost never fires.
#
# The rule is also self-limiting. A genuinely anaphoric question ("وكم في غزة؟")
# can never be IN the cache, because nobody asks it as a first turn, so it can
# never be served from it.
#
# The residual risk is real, not theoretical: the same words can mean something
# narrower deep in a conversation ("كم عدد القضايا؟" after five turns about
# Gaza) than they did cold. The answer is still written with the full history in
# the prompt, and the SQL is shown in the UI, but neither is a guarantee. Set
# this false for the strict first-turn-only behaviour.
QUERY_CACHE_FOLLOW_UPS = os.getenv("QUERY_CACHE_FOLLOW_UPS", "true").lower() == "true"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
# Namespaces every key this feature owns, so `clear()` can find exactly its own
# keys and a Redis shared with anything else is never touched.
QUERY_CACHE_PREFIX = os.getenv("QUERY_CACHE_PREFIX", "querycache")
# A ceiling, not a tuning knob. The realistic number of distinct questions this
# system will ever see is in the hundreds; this only stops an automated client
# growing the keyspace without bound. Oldest entries are evicted first.
QUERY_CACHE_MAX_ENTRIES = int(os.getenv("QUERY_CACHE_MAX_ENTRIES", "5000"))
# Optional expiry per entry, 0 to disable (the default). Invalidation here is
# event-driven — the cache is cleared when a table is documented or the glossary
# syncs — so a TTL is a backstop for changes that arrive through neither, such
# as a column edited directly in MySQL without CDC running.
QUERY_CACHE_TTL_SECONDS = int(os.getenv("QUERY_CACHE_TTL_SECONDS", "0"))
# How long to wait on Redis before giving up and treating the turn as a miss.
# Deliberately short: the cache exists to save seconds, so it must never be able
# to ADD them. A question answered without the cache is fine; one that stalls
# waiting for it is not.
REDIS_TIMEOUT_SECONDS = float(os.getenv("REDIS_TIMEOUT_SECONDS", "2.0"))

# --- Legal concept file sync --------------------------------------------------
# The JSON file a user edits, and the snapshot that makes the sync three-way.
# Without the snapshot, "removed from the file" and "added to Qdrant" are
# indistinguishable, and so are "removed from Qdrant" and "added to the file" —
# a two-way diff would guess, and half its guesses delete data.
CONCEPTS_FILE = _path_from_env("CONCEPTS_FILE", "data/concepts.json")
CONCEPTS_SYNC_STATE_FILE = _path_from_env(
    "CONCEPTS_SYNC_STATE_FILE", "data/.concepts-sync-state.json"
)
# Refuse any sync that would delete more than this fraction of either side, and
# raise a warning instead. This is the rail against the failure that actually
# happens: Qdrant restarts on an empty volume, every concept looks deleted, and
# an unguarded reconcile empties concepts.json to match. Set to 1.0 to disable.
CONCEPT_SYNC_DELETE_RATIO = float(os.getenv("CONCEPT_SYNC_DELETE_RATIO", "0.30"))
# Below this count the ratio is not applied — going from 2 concepts to 1 is a
# 50% delete but obviously fine, and a ratio alone would make small collections
# impossible to edit.
CONCEPT_SYNC_DELETE_FLOOR = int(os.getenv("CONCEPT_SYNC_DELETE_FLOOR", "4"))

# --- Qdrant deletion watcher --------------------------------------------------
# Qdrant runs locally in Docker, so its logs are the only notification available
# when someone deletes points through the dashboard or the raw API.
#
# The logs say a delete HAPPENED, never WHAT was deleted: point deletes appear
# only as an actix access line ("POST /collections/legal_concepts/points/delete")
# with no request body. So the watcher is a trigger for a full reconcile, never
# a source of identity. Collection drops do get a named line.
#
# Only REST (6333) is logged. A delete issued over gRPC (6334) is invisible —
# qdrant-client defaults to REST, so our own writes are covered.
QDRANT_WATCH_ENABLED = os.getenv("QDRANT_WATCH_ENABLED", "true").lower() == "true"
QDRANT_CONTAINER = os.getenv("QDRANT_CONTAINER", "texttosql-qdrant")
# Coalescing window. A single "delete these 12 points" call from the dashboard
# arrives as one line, but a script deleting them one at a time arrives as 12 —
# and reconciling after each would re-embed the whole file a dozen times.
QDRANT_WATCH_DEBOUNCE_SECONDS = float(os.getenv("QDRANT_WATCH_DEBOUNCE_SECONDS", "3.0"))

# --- Debezium CDC -------------------------------------------------------------
# Debezium Server tails the MySQL binlog and POSTs change events to
# /api/cdc/events (its HTTP sink). See debezium/application.properties.
CDC_ENABLED = os.getenv("CDC_ENABLED", "true").lower() == "true"
# How long a newly-seen table waits before it is documented. A CREATE TABLE
# reaches us the instant it commits, when the table is still empty — and sample
# rows are the reviewer's strongest evidence for telling a real domain table from
# somebody's scratch import. Waiting lets the INSERTs that follow arrive first.
CDC_DEBOUNCE_SECONDS = float(os.getenv("CDC_DEBOUNCE_SECONDS", "20"))
# Hard ceiling on that wait. A table that keeps receiving writes would otherwise
# have its debounce extended forever and never get documented at all.
CDC_MAX_WAIT_SECONDS = float(os.getenv("CDC_MAX_WAIT_SECONDS", "120"))
# What the automated path documented, kept so a human can read it afterwards —
# both the tables it flagged (not indexed, needs a person) and the ones it was
# happy with (already indexed, but nobody has read the description). Unlike the
# manual scan flow, where the browser holds the run state between scan and
# approve, nobody is watching when CDC fires, so this has to outlive the request.
REVIEW_QUEUE_FILE = _path_from_env("REVIEW_QUEUE_FILE", "data/review-queue.json")

# --- Activity log -------------------------------------------------------------
# Everything the automated paths do lands here and is streamed to the Activity
# tab. Append-only JSONL: it is a log, it is read newest-first, and a database
# for it would be the only stateful dependency in the backend.
ACTIVITY_LOG_FILE = _path_from_env("ACTIVITY_LOG_FILE", "data/activity.jsonl")
# Events held in memory for instant reads. Older ones stay on disk and are read
# back from the file when the tab asks for more.
ACTIVITY_RING_SIZE = int(os.getenv("ACTIVITY_RING_SIZE", "500"))

# --- CORS (frontend dev server) ----------------------------------------------
CORS_ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:5173").split(",")
