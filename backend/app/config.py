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

# --- LM Studio (LLM + embeddings, OpenAI-compatible local server) -----------
# Enable via LM Studio > Developer > Start Server (default port 1234), with
# both the Gemma 4 E4B and BGE-M3 models loaded. Model identifiers below must
# match exactly what LM Studio reports at GET /v1/models.
LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-bge-m3")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemma-4-e4b")

# --- Query execution safety ---------------------------------------------------
MAX_RESULT_ROWS = int(os.getenv("MAX_RESULT_ROWS", "200"))
SQL_STATEMENT_TIMEOUT_SECONDS = int(os.getenv("SQL_STATEMENT_TIMEOUT_SECONDS", "10"))

# --- CORS (frontend dev server) ----------------------------------------------
CORS_ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:5173").split(",")
