import os

from dotenv import load_dotenv

load_dotenv()

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "schema_docs")

# LM Studio's local server (OpenAI-compatible). Enable it via
# LM Studio > Developer > Start Server (default port 1234).
LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-bge-m3")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))
