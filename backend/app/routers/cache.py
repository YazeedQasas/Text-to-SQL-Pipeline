"""The repeated-question cache, for the Cache panel.

Read as a snapshot rather than a stream: unlike the activity log, nothing here
happens while nobody is asking. The panel refreshes when it is opened and after
a question is answered, which is the only time the contents can have changed.

Entries are returned with their question text and SQL in full. This is an admin
surface — the point of it is to be able to see WHICH question was reused and
WHAT it runs, which a hashed key on its own cannot tell anyone.
"""

from fastapi import APIRouter, HTTPException, Query

from app.services import query_cache

router = APIRouter(prefix="/api/cache", tags=["cache"])


@router.get("")
async def get_cache(limit: int = Query(200, ge=1, le=1000)) -> dict:
    return {
        "stats": await query_cache.stats(),
        "entries": await query_cache.listing(limit=limit),
    }


@router.delete("")
async def clear_cache() -> dict:
    """Empty the cache.

    Nothing is lost that cannot be regenerated — the next time each question is
    asked it is answered from scratch and stored again.

    The hit/miss counters are deliberately left alone. They measure whether this
    feature is worth keeping, which is a question about the whole deployment;
    resetting them every time somebody empties the cache would keep restarting
    that measurement from zero and never accumulate an answer.
    """
    return {"cleared": await query_cache.clear()}


@router.delete("/{key}")
async def remove_entry(key: str) -> dict:
    """Drop one entry, for when a single stored query is wrong.

    The cache-wide invalidation hooks cover schema and glossary changes. This is
    for the other case: a question whose SQL was accepted by the guard, ran
    without error, and still answered the wrong thing.
    """
    if not await query_cache.remove(key):
        raise HTTPException(status_code=404, detail="No cached question with that key.")
    return {"removed": key}
