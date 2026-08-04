import os

from dotenv import load_dotenv

load_dotenv()

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

# --- Domain glossary (ingestion/concepts.py) ---------------------------------
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
# re-run of ingest_concepts.py take effect without restarting the backend.
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

# --- CORS (frontend dev server) ----------------------------------------------
CORS_ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:5173").split(",")
