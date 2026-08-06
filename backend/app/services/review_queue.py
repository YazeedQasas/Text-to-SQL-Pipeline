"""What the automated path documented, kept so a human can look at it afterwards.

The manual catalog flow needs nothing like this: the browser holds the scan
results between Scan and Approve, so a table stays on screen until the operator
deals with it. The CDC flow has no browser and no operator — it fires whenever
someone runs a CREATE TABLE — so what it produced has to wait somewhere that
outlives the request.

Two kinds of entry live here, and the difference is one field:

- `indexed=False` — the reviewer flagged it, so it was NOT written to Qdrant.
  It needs a human before it can be queried at all.
- `indexed=True`  — the reviewer was happy and it IS in Qdrant already. It works
  right now; the entry exists so the description the model wrote can still be
  read and corrected.

The second kind is why this is a review queue rather than a quarantine. A clean
verdict means "not obviously wrong", not "well written", and without this the
only trace of an automatically written description was one line in the activity
log. A scan cannot bring it back either — a scan reports tables that DIFFER from
what is indexed, and an auto-documented table matches by definition.

Entries are keyed by table name: re-documenting a table replaces its entry
rather than adding a second one, because "what does this table need?" has one
current answer, not a history.
"""

import json
import logging
import time
from pathlib import Path

from app.config import REVIEW_QUEUE_FILE

logger = logging.getLogger(__name__)


def _path() -> Path:
    return Path(REVIEW_QUEUE_FILE)


def load() -> dict[str, dict]:
    """Everything waiting to be looked at, keyed by table name."""
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 — a corrupt file must not wedge the CDC loop
        logger.warning("Could not read the review queue at %s: %s", path, exc)
        return {}


def _save(entries: dict[str, dict]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # `default=str` is a backstop, not the fix: sample rows are converted to
    # JSON-safe values in introspect.json_safe_row before they ever get here.
    # It exists so one exotic column type can never cost a whole documentation
    # run, which is what "Object of type Decimal is not JSON serializable" did.
    body = json.dumps(entries, ensure_ascii=False, indent=2, default=str)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(body + "\n", encoding="utf-8")
    temp_path.replace(path)


def add(
    table_name: str,
    change_type: str,
    summary: str,
    doc: dict,
    verdict: dict,
    sample_rows: list[dict],
    row_count: int,
    indexed: bool,
) -> dict:
    """Record what was documented. `indexed` says whether it reached Qdrant."""
    entry = {
        "table_name": table_name,
        "change_type": change_type,
        "summary": summary,
        "doc": doc,
        "verdict": verdict,
        "sample_rows": sample_rows,
        "row_count": row_count,
        "indexed": indexed,
        "detected_at": time.time(),
    }
    entries = load()
    entries[table_name] = entry
    _save(entries)
    return entry


def remove(table_name: str) -> bool:
    entries = load()
    if table_name not in entries:
        return False
    del entries[table_name]
    _save(entries)
    return True


def listing() -> list[dict]:
    """Everything waiting.

    Flagged entries first, then oldest first within each group — an entry that
    is not yet queryable is more urgent than one that merely might be worded
    better.
    """
    entries = load().values()
    return sorted(entries, key=lambda e: (bool(e.get("indexed")), e.get("detected_at", 0.0)))


def count(indexed: bool | None = None) -> int:
    """How many entries, optionally of one kind.

    `count(indexed=False)` is what the nav badge shows: only tables that are not
    queryable yet are worth interrupting someone for.
    """
    entries = load().values()
    if indexed is None:
        return len(entries)
    return sum(1 for entry in entries if bool(entry.get("indexed")) is indexed)
