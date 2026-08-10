"""Tests for the Redis-backed repeated-question cache.

Two properties matter, and they pull in opposite directions.

WIDE ENOUGH: questions that differ only in how they were typed — a question
mark, a diacritic, the definite article — have to reach the same entry, or the
cache never fires on real input.

NARROW ENOUGH: questions that differ in a year, a place or a negation must NOT
collide. Those are one token apart and mean entirely different things, and a
collision there does not fail loudly — it runs someone else's query and reports
a confident number for a question nobody asked.

A third property is specific to putting this in Redis: the cache is an
optimisation, so a Redis that is down must degrade to "every question is a
miss", never to an error. The last section covers that.

No pytest-asyncio in this project, so the async surface is driven with
asyncio.run() and a shared fakeredis server holds state between calls.
"""

import asyncio

import fakeredis
import fakeredis.aioredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.services import query_cache


@pytest.fixture(autouse=True)
def fake_redis():
    """A fresh in-process Redis per test.

    The client is rebuilt for every call rather than held, because each
    asyncio.run() below opens its own event loop and a client bound to a closed
    one cannot be reused. The FakeServer is what actually carries the data
    across those loops.
    """
    server = fakeredis.FakeServer()
    query_cache.set_client(fakeredis.aioredis.FakeRedis(server=server, decode_responses=True))
    yield server
    query_cache.set_client(None)


def _run(server, coro_factory):
    """Run one coroutine against the shared fake server."""

    async def main():
        query_cache.set_client(
            fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
        )
        return await coro_factory()

    return asyncio.run(main())


# --- What must match ----------------------------------------------------------


def test_a_trailing_question_mark_is_the_same_question():
    assert query_cache.cache_key("ما هي القضايا المدورة؟") == query_cache.cache_key(
        "ما هي القضايا المدورة"
    )


def test_the_definite_article_and_ta_marbuta_fold_together():
    """"القضايا المدورة" and "قضايا مدورة" ask for the same thing."""
    assert query_cache.cache_key("ما هي القضايا المدورة؟") == query_cache.cache_key(
        "ما هي قضايا مدورة"
    )


def test_extra_whitespace_is_the_same_question():
    assert query_cache.cache_key("كم عدد   القضايا ؟") == query_cache.cache_key("كم عدد القضايا؟")


# --- What must NOT match ------------------------------------------------------


def test_different_years_are_different_questions():
    """The case this cache would be dangerous without: digits survive folding."""
    assert query_cache.cache_key("كم قضية في 2023؟") != query_cache.cache_key("كم قضية في 2024؟")


def test_negation_is_a_different_question():
    assert query_cache.cache_key("ما هي القضايا المدورة؟") != query_cache.cache_key(
        "ما هي القضايا غير المدورة؟"
    )


def test_different_place_names_are_different_questions():
    assert query_cache.cache_key("قضايا محكمة رام الله") != query_cache.cache_key(
        "قضايا محكمة نابلس"
    )


# --- Storing and reading ------------------------------------------------------


def test_a_stored_question_comes_back_with_its_sql_and_tables(fake_redis):
    async def scenario():
        await query_cache.store("ما هي القضايا المدورة؟", "SELECT 1", ["cases"], ["مدورة"])
        return await query_cache.lookup("ما هي قضايا مدورة")

    entry = _run(fake_redis, scenario)
    assert entry is not None
    assert entry["sql"] == "SELECT 1"
    assert entry["table_names"] == ["cases"]
    assert entry["concept_terms"] == ["مدورة"]


def test_an_unknown_question_is_a_miss(fake_redis):
    assert _run(fake_redis, lambda: query_cache.lookup("سؤال لم يسأل من قبل")) is None


def test_a_question_with_no_content_words_is_never_stored(fake_redis):
    async def scenario():
        stored = await query_cache.store("؟؟؟", "SELECT 1", ["cases"])
        return stored, await query_cache.stats()

    stored, stats = _run(fake_redis, scenario)
    assert stored is None
    assert stats["entries"] == 0


def test_empty_sql_is_never_stored(fake_redis):
    async def scenario():
        stored = await query_cache.store("سؤال", "   ", ["cases"])
        return stored, await query_cache.stats()

    stored, stats = _run(fake_redis, scenario)
    assert stored is None
    assert stats["entries"] == 0


# --- Counters -----------------------------------------------------------------


def test_hits_and_misses_are_counted(fake_redis):
    async def scenario():
        await query_cache.store("سؤال أول", "SELECT 1", ["cases"])
        await query_cache.lookup("سؤال أول")
        await query_cache.lookup("سؤال أول")
        await query_cache.lookup("سؤال آخر")
        return await query_cache.stats()

    stats = _run(fake_redis, scenario)
    assert stats["hits"] == 2
    assert stats["misses"] == 1
    assert stats["hit_rate"] == pytest.approx(2 / 3)


def test_hit_rate_is_zero_before_any_question(fake_redis):
    assert _run(fake_redis, query_cache.stats)["hit_rate"] == 0.0


def test_use_counts_track_reuse(fake_redis):
    async def scenario():
        await query_cache.store("سؤال", "SELECT 1", ["cases"])
        await query_cache.lookup("سؤال")
        return await query_cache.lookup("سؤال")

    assert _run(fake_redis, scenario)["uses"] == 2


def test_counters_survive_a_backend_restart(fake_redis):
    """The reason for moving this off a file: the hit rate now measures the life
    of the cache rather than the life of the process."""

    async def first():
        await query_cache.store("سؤال", "SELECT 1", ["cases"])
        await query_cache.lookup("سؤال")

    _run(fake_redis, first)
    # A new client and a new event loop — a restarted worker, same Redis.
    stats = _run(fake_redis, query_cache.stats)

    assert stats["hits"] == 1
    assert stats["entries"] == 1


# --- Invalidation -------------------------------------------------------------


def test_clear_empties_the_cache_and_reports_the_count(fake_redis):
    async def scenario():
        await query_cache.store("سؤال أول", "SELECT 1", ["cases"])
        await query_cache.store("سؤال ثان", "SELECT 2", ["cases"])
        removed = await query_cache.clear()
        return removed, await query_cache.stats(), await query_cache.lookup("سؤال أول")

    removed, stats, entry = _run(fake_redis, scenario)
    assert removed == 2
    assert stats["entries"] == 0
    assert entry is None


def test_clear_leaves_other_keys_in_the_same_redis_alone(fake_redis):
    """The Redis behind this may not be ours — so no FLUSHDB, ever."""

    async def scenario():
        client = query_cache.get_client()
        await client.set("someone-elses-key", "value")
        await query_cache.store("سؤال", "SELECT 1", ["cases"])
        await query_cache.clear()
        return await client.get("someone-elses-key")

    assert _run(fake_redis, scenario) == "value"


def test_one_entry_can_be_removed(fake_redis):
    async def scenario():
        stored = await query_cache.store("سؤال أول", "SELECT 1", ["cases"])
        await query_cache.store("سؤال ثان", "SELECT 2", ["cases"])
        first = await query_cache.remove(stored["key"])
        again = await query_cache.remove(stored["key"])
        return first, again, await query_cache.stats()

    first, again, stats = _run(fake_redis, scenario)
    assert first is True
    assert again is False
    assert stats["entries"] == 1


# --- Bounds -------------------------------------------------------------------


def test_the_oldest_entries_are_dropped_at_the_cap(fake_redis, monkeypatch):
    monkeypatch.setattr(query_cache, "QUERY_CACHE_MAX_ENTRIES", 3)

    async def scenario():
        for i in range(5):
            await query_cache.store(f"سؤال رقم {i}", f"SELECT {i}", ["cases"])
        return (
            await query_cache.stats(),
            await query_cache.lookup("سؤال رقم 0"),
            await query_cache.lookup("سؤال رقم 4"),
        )

    stats, oldest, newest = _run(fake_redis, scenario)
    assert stats["entries"] == 3
    assert oldest is None
    assert newest is not None


def test_listing_is_newest_first(fake_redis):
    async def scenario():
        await query_cache.store("سؤال أول", "SELECT 1", ["cases"])
        await query_cache.store("سؤال ثان", "SELECT 2", ["cases"])
        return await query_cache.listing()

    assert [e["question"] for e in _run(fake_redis, scenario)] == ["سؤال ثان", "سؤال أول"]


def test_an_expired_entry_is_dropped_from_the_index(fake_redis):
    """A TTL expires the hash; Redis knows nothing about the sorted set pointing
    at it, so the pointer has to be cleaned up on the way past."""

    async def scenario():
        stored = await query_cache.store("سؤال", "SELECT 1", ["cases"])
        # Expire the hash the way a TTL would, leaving the index member behind.
        await query_cache.get_client().delete(query_cache._entry_key(stored["key"]))
        missed = await query_cache.lookup("سؤال")
        return missed, await query_cache.stats()

    missed, stats = _run(fake_redis, scenario)
    assert missed is None
    assert stats["entries"] == 0


# --- Redis being down ---------------------------------------------------------


class _DeadRedis:
    """Every call raises, the way an unreachable server behaves."""

    def __getattr__(self, _name):
        def boom(*_args, **_kwargs):
            raise RedisConnectionError("Connection refused")

        return boom


def test_a_dead_redis_makes_every_question_a_miss(monkeypatch):
    monkeypatch.setattr(query_cache, "_client", _DeadRedis())

    assert asyncio.run(query_cache.lookup("سؤال")) is None
    assert asyncio.run(query_cache.store("سؤال", "SELECT 1", ["cases"])) is None
    assert asyncio.run(query_cache.remove("whatever")) is False
    assert asyncio.run(query_cache.clear()) == 0
    assert asyncio.run(query_cache.listing()) == []


def test_a_dead_redis_is_reported_rather_than_hidden(monkeypatch):
    """The panel has to be able to say "Redis is down" instead of showing a
    plausible-looking hit rate of zero."""
    monkeypatch.setattr(query_cache, "_client", _DeadRedis())

    assert asyncio.run(query_cache.stats())["available"] is False


def test_ping_reports_a_dead_redis_without_raising(monkeypatch):
    monkeypatch.setattr(query_cache, "_client", _DeadRedis())

    assert asyncio.run(query_cache.ping()) is False
