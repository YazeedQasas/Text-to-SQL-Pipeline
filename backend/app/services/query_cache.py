"""SQL already written for a question that has been asked before, kept in Redis.

Two questions that differ only in punctuation, diacritics or the definite
article ask for exactly the same thing:

    ما هي القضايا المدورة؟
    ما هي قضايا مدورة

Answering the second one from scratch costs an embedding call and an LLM
completion — measured, the embedding alone is ~2.7s (see pipeline.py), and SQL
generation is a second streamed completion on top. This module lets the second
question skip both.

WHAT IS STORED IS THE SQL, NEVER THE RESULT
-------------------------------------------
A cache hit re-executes the stored SQL against MySQL and regenerates the answer
from whatever comes back today. Storing rows or answer text would mean serving
last week's case count as though it were current, and a confidently stale number
out of a legal database is worse than a slow correct one.

Table names are stored alongside the SQL because three things downstream need
them: the `retrieved_tables` field of the response, the carry-over that lets a
follow-up question keep its subject (pipeline.py), and the context meter. Only
the NAMES are stored — the documents themselves are re-fetched live, so a table
that CDC has since re-documented can never serve its old description from here.

MATCHING IS LEXICAL, NOT SEMANTIC
---------------------------------
The key is a hash of the question after arabic.normalize (diacritics dropped,
hamza forms folded, the definite article removed) and arabic.words (punctuation
dropped). Nothing is matched by meaning.

That is a deliberate first step, not an oversight. A semantic layer cannot skip
the embedding — it needs the vector to do the lookup — so it can only ever save
the generation call, and it introduces a failure this layer cannot have: the
tokens that decide a SQL query are exactly the ones an embedding compresses
away. "قضايا 2023" and "قضايا 2024" sit a hair apart in vector space and need
completely different queries, and a wrong query runs cleanly and reports a
confident number for a question nobody asked. Whether that risk is worth taking
is a question for the hit-rate figures this module records, not for a guess.

KEYSPACE
--------
    <prefix>:q:<sha256>   HASH   one cached question
    <prefix>:index        ZSET   member = sha256, score = created_at
    <prefix>:hits         INT    counters behind the panel's hit rate
    <prefix>:misses       INT
    <prefix>:cleared_at   FLOAT  when the cache was last emptied

The sorted set exists because a cache needs two orderings a plain keyspace
cannot give cheaply: newest-first for the panel, and oldest-first for eviction.
SCAN over the keyspace would supply neither, and would walk every key to do it.

REDIS IS NOT LOAD-BEARING
-------------------------
Every operation below fails soft. A Redis that is down, unreachable or slow
turns every question into a cache miss and the pipeline runs exactly as it did
before this feature existed. This is the whole reason the timeout is two seconds
rather than the client default: a cache that exists to save seconds must never
be able to add them.

The handlers catch Exception rather than RedisError, and that width is
deliberate. redis-py does not funnel everything through its own hierarchy: a
connection whose event loop has gone surfaces as RuntimeError("Event loop is
closed"), and a half-torn-down transport as AttributeError on None — neither is
a RedisError or an OSError, and both were reached in practice here. Since no
failure of this module should ever reach the user, the honest rule is that
nothing escapes it at all.
"""

import hashlib
import json
import logging
import time

import redis.asyncio as redis

from app.config import (
    QUERY_CACHE_MAX_ENTRIES,
    QUERY_CACHE_PREFIX,
    QUERY_CACHE_TTL_SECONDS,
    REDIS_TIMEOUT_SECONDS,
    REDIS_URL,
)
from app.services import arabic

logger = logging.getLogger(__name__)

_client: redis.Redis | None = None


# --- Keys ---------------------------------------------------------------------


def _entry_key(key: str) -> str:
    return f"{QUERY_CACHE_PREFIX}:q:{key}"


def _index_key() -> str:
    return f"{QUERY_CACHE_PREFIX}:index"


def _counter_key(name: str) -> str:
    return f"{QUERY_CACHE_PREFIX}:{name}"


# --- Connection ---------------------------------------------------------------


def get_client() -> redis.Redis:
    """The shared client, created on first use.

    `decode_responses` so everything comes back as str — the payloads here are
    Arabic question text and SQL, and decoding at every call site would be noise.
    Connecting is lazy: the constructor opens no socket, so a Redis that is down
    at import time costs nothing until something actually asks for a key.
    """
    global _client
    if _client is None:
        _client = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=REDIS_TIMEOUT_SECONDS,
            socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
        )
    return _client


def set_client(client: redis.Redis | None) -> None:
    """Replace the client. For tests, which inject a fakeredis instance."""
    global _client
    _client = client


async def ping() -> bool:
    """Whether Redis is reachable. Called once at startup, for the log line."""
    try:
        await get_client().ping()
        return True
    except Exception as exc:  # noqa: BLE001 — nothing here may reach the user
        logger.warning(
            "Redis is not reachable at %s (%s). Every question will be a cache "
            "miss; nothing else is affected.",
            REDIS_URL,
            exc,
        )
        return False


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:  # noqa: BLE001, S110 — shutting down anyway
            pass
        _client = None


# --- Keying -------------------------------------------------------------------


def cache_key(question: str) -> str:
    """The lookup key for a question, or "" if it has no content words.

    normalize() folds spelling but leaves punctuation attached — ؟ is U+061F,
    inside the Arabic block — so words() has to run after it to drop the
    question mark. Doing only one of the two leaves "…المدورة؟" and "…المدورة"
    as different keys, which is the exact case this cache exists for.
    """
    tokens = arabic.words(arabic.normalize(question))
    if not tokens:
        return ""
    return hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest()


def _to_entry(key: str, raw: dict[str, str]) -> dict:
    """One Redis hash as the dict the rest of the app expects.

    Hash values are flat strings, so the two list fields are JSON on the way in
    and out. A malformed value yields an empty list rather than raising: a cache
    entry is not worth failing a question over, and an entry whose table names
    are gone is rejected by the caller anyway.
    """

    def _list(field: str) -> list[str]:
        try:
            value = json.loads(raw.get(field) or "[]")
            return value if isinstance(value, list) else []
        except json.JSONDecodeError:
            return []

    return {
        "key": key,
        "question": raw.get("question", ""),
        "sql": raw.get("sql", ""),
        "table_names": _list("table_names"),
        "concept_terms": _list("concept_terms"),
        "created_at": float(raw.get("created_at") or 0.0),
        "uses": int(raw.get("uses") or 0),
    }


# --- Reads and writes ---------------------------------------------------------


async def lookup(question: str) -> dict | None:
    """The stored entry for this question, or None.

    Counts every call as a hit or a miss — this is the figure that decides
    whether the cache is worth keeping, so it has to cover every question that
    reaches the cache, not only the interesting ones.
    """
    key = cache_key(question)
    if not key:
        return None

    client = get_client()
    try:
        raw = await client.hgetall(_entry_key(key))

        if not raw:
            # An entry that expired under a TTL leaves its index member behind,
            # because Redis expires the hash and knows nothing about the sorted
            # set pointing at it. Clean it up here rather than letting the panel
            # report entries that are no longer there.
            await client.zrem(_index_key(), key)
            await client.incr(_counter_key("misses"))
            return None

        # Pipelined: two round trips become one, on the path that has to be the
        # cheapest thing in the request.
        async with client.pipeline(transaction=False) as pipe:
            pipe.incr(_counter_key("hits"))
            pipe.hincrby(_entry_key(key), "uses", 1)
            _, uses = await pipe.execute()

        entry = _to_entry(key, raw)
        entry["uses"] = int(uses)
        return entry
    except Exception as exc:  # noqa: BLE001 — nothing here may reach the user
        logger.warning("Query cache lookup failed, treating as a miss: %s", exc)
        return None


async def store(
    question: str,
    sql: str,
    table_names: list[str],
    concept_terms: list[str] | None = None,
) -> dict | None:
    """Record the SQL a question resolved to. Callers must only store successes.

    A run that the model declined, that the SQL guard rejected, or that MySQL
    errored on tells us nothing about whether the retrieval behind it was right,
    so there is nothing worth keeping.
    """
    key = cache_key(question)
    if not key or not sql.strip():
        return None

    created_at = time.time()
    entry = {
        "key": key,
        "question": question,
        "sql": sql,
        "table_names": list(table_names),
        # Terms, not definitions: the glossary entry behind a term is re-fetched
        # live on a hit, so an edited definition or SQL fragment takes effect
        # here the same turn it takes effect everywhere else.
        "concept_terms": list(concept_terms or []),
        "created_at": created_at,
        "uses": 0,
    }

    client = get_client()
    try:
        async with client.pipeline(transaction=True) as pipe:
            pipe.hset(
                _entry_key(key),
                mapping={
                    "question": question,
                    "sql": sql,
                    "table_names": json.dumps(list(table_names), ensure_ascii=False),
                    "concept_terms": json.dumps(list(concept_terms or []), ensure_ascii=False),
                    "created_at": repr(created_at),
                    "uses": 0,
                },
            )
            if QUERY_CACHE_TTL_SECONDS > 0:
                pipe.expire(_entry_key(key), QUERY_CACHE_TTL_SECONDS)
            pipe.zadd(_index_key(), {key: created_at})
            await pipe.execute()

        await _evict_if_full(client)
        return entry
    except Exception as exc:  # noqa: BLE001 — nothing here may reach the user
        logger.warning("Could not write to the query cache: %s", exc)
        return None


async def _evict_if_full(client: redis.Redis) -> None:
    """Drop the oldest entries once the cap is passed.

    By creation time rather than last use. An LRU policy would need the score
    rewritten on every hit, which turns the cheapest path in the request into a
    write — and the cap exists to bound a runaway client, not to keep a working
    set hot. Oldest-first is the honest reading of what it is for.
    """
    if QUERY_CACHE_MAX_ENTRIES <= 0:
        return

    excess = await client.zcard(_index_key()) - QUERY_CACHE_MAX_ENTRIES
    if excess <= 0:
        return

    doomed = await client.zrange(_index_key(), 0, excess - 1)
    if not doomed:
        return

    async with client.pipeline(transaction=True) as pipe:
        pipe.delete(*(_entry_key(key) for key in doomed))
        pipe.zrem(_index_key(), *doomed)
        await pipe.execute()


async def remove(key: str) -> bool:
    client = get_client()
    try:
        async with client.pipeline(transaction=True) as pipe:
            pipe.delete(_entry_key(key))
            pipe.zrem(_index_key(), key)
            deleted, _ = await pipe.execute()
        return bool(deleted)
    except Exception as exc:  # noqa: BLE001 — nothing here may reach the user
        logger.warning("Could not remove a cached question: %s", exc)
        return False


async def clear() -> int:
    """Empty the cache. Returns how many entries went.

    Called whenever the schema or the glossary changes: a stored query was
    written against the tables and terms as they were, and both of those can
    change under it. Wiping everything rather than tracking which entries touch
    which tables is deliberate — the cache is small, it refills on its own, and
    a wrong invalidation rule serves wrong SQL silently.

    Only keys under this feature's prefix are touched, never FLUSHDB: the Redis
    behind this may not be ours alone.
    """
    client = get_client()
    try:
        keys = await client.zrange(_index_key(), 0, -1)
        async with client.pipeline(transaction=True) as pipe:
            if keys:
                pipe.delete(*(_entry_key(key) for key in keys))
            pipe.delete(_index_key())
            pipe.set(_counter_key("cleared_at"), repr(time.time()))
            await pipe.execute()
        return len(keys)
    except Exception as exc:  # noqa: BLE001 — nothing here may reach the user
        logger.warning("Could not clear the query cache: %s", exc)
        return 0


# --- Reporting ----------------------------------------------------------------


async def stats() -> dict:
    """Counters for the cache panel.

    Unlike the file-backed version this replaced, these survive a restart: they
    live in Redis rather than in the process, so the hit rate is measured over
    the life of the cache instead of the life of the worker. `available` is what
    lets the panel say "Redis is down" instead of quietly showing zeroes.
    """
    client = get_client()
    try:
        async with client.pipeline(transaction=False) as pipe:
            pipe.zcard(_index_key())
            pipe.get(_counter_key("hits"))
            pipe.get(_counter_key("misses"))
            pipe.get(_counter_key("cleared_at"))
            entries, hits, misses, cleared_at = await pipe.execute()

        hits, misses = int(hits or 0), int(misses or 0)
        total = hits + misses
        return {
            "available": True,
            "entries": int(entries or 0),
            "max_entries": QUERY_CACHE_MAX_ENTRIES,
            "hits": hits,
            "misses": misses,
            "hit_rate": (hits / total) if total else 0.0,
            "cleared_at": float(cleared_at or 0.0),
        }
    except Exception as exc:  # noqa: BLE001 — nothing here may reach the user
        logger.warning("Could not read query cache stats: %s", exc)
        return {
            "available": False,
            "entries": 0,
            "max_entries": QUERY_CACHE_MAX_ENTRIES,
            "hits": 0,
            "misses": 0,
            "hit_rate": 0.0,
            "cleared_at": 0.0,
        }


async def listing(limit: int = 200) -> list[dict]:
    """Entries for the panel, most recently stored first."""
    client = get_client()
    try:
        keys = await client.zrevrange(_index_key(), 0, max(0, limit - 1))
        if not keys:
            return []

        async with client.pipeline(transaction=False) as pipe:
            for key in keys:
                pipe.hgetall(_entry_key(key))
            raws = await pipe.execute()

        entries = [_to_entry(key, raw) for key, raw in zip(keys, raws) if raw]

        # Self-heal: a member whose hash has expired is a pointer to nothing.
        expired = [key for key, raw in zip(keys, raws) if not raw]
        if expired:
            await client.zrem(_index_key(), *expired)

        return entries
    except Exception as exc:  # noqa: BLE001 — nothing here may reach the user
        logger.warning("Could not list cached questions: %s", exc)
        return []
