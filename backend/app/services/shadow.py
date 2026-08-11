"""Shadow verification: proving a cache key's merges rather than arguing them.

Every other rail in this feature encodes a failure someone already found. The
key whitelist answers the operator merge; rail 3 in services/intent.py answers
the المدعي/المدعى merge; the concept ID in the key answers glossary absorption.
Each was discovered by a person staring at Arabic and guessing well.

This is the one mechanism that can catch the merge nobody thought of.

HOW
---
A coarse key is a claim: "every question that lands here wants the same plan."
The claim is checkable, because the full pipeline can still be run and its SQL
is the reference. So for the first SHADOW_CONFIRMATIONS hits on a key, run BOTH
— the cached plan and the full pipeline — and compare them at the plan level
(services/plan_shape.py). Agreement promotes the key toward trusted; a single
disagreement quarantines it.

That makes merge-safety a measurement, held to the same standard as
CONCEPT_SCORE_THRESHOLD, which was calibrated over a probe rather than reasoned
about.

WHAT THE USER SEES WHILE A KEY IS BEING VERIFIED
------------------------------------------------
Nothing different, and nothing slower than a miss. A shadowed hit runs the full
pipeline anyway, so it costs exactly what the question would have cost without
any cache — and the ANSWER SERVED IS THE REFERENCE ONE, never the adapted one.
The adapted query is only ever compared, not executed, until its key is trusted.
That is the property that makes it safe to switch this on in front of real
users: an unverified key cannot produce a wrong answer, it can only fail to
produce a fast one.

QUARANTINE IS ONE STRIKE
------------------------
Not a ratio. A key that produced a wrong plan once has demonstrated that it
merges questions it should not; the number of times it also produced a right one
says nothing about that. A quarantined key is never read again until the cache
is invalidated, at which point it is gone with everything else.

FAILURE POLICY
--------------
Fails toward verifying, never toward trusting. If the ledger cannot be read or
written, every key looks unverified, every hit gets shadowed, and the system
behaves as though the cache were disabled — slow and correct. Nothing in this
module may raise into a request.
"""

import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.config import (
    SHADOW_CONFIRMATIONS,
    SHADOW_LEDGER_FILE,
    SHADOW_RING_SIZE,
    SHADOW_VERIFY_ENABLED,
)
from app.services import plan_shape

logger = logging.getLogger(__name__)

VERDICT_CONFIRMED = "confirmed"
VERDICT_DIVERGED = "diverged"

# Reasons a key can be quarantined, kept apart so the panel can say which.
REASON_SHAPE = "plan shape diverged from the full pipeline"
REASON_OPERATOR = "an operator the question carried is missing from the plan's SQL"


@dataclass
class Observation:
    """One shadowed hit: what was compared, and how it came out."""

    ts: float
    key: str
    question: str
    verdict: str
    adapted_sql: str = ""
    reference_sql: str = ""
    differences: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class KeyState:
    """What has been observed about one cache key.

    `confirmations` only ever counts agreements. A quarantined key keeps its
    count so the panel can show that it worked N times before it did not —
    which is exactly the shape of a merge that is right for the common question
    and wrong for a rarer one, and the most valuable thing this ledger records.
    """

    key: str
    confirmations: int = 0
    divergences: int = 0
    quarantined: bool = False
    reason: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0

    @property
    def trusted(self) -> bool:
        return not self.quarantined and self.confirmations >= SHADOW_CONFIRMATIONS

    def to_dict(self) -> dict:
        return {**asdict(self), "trusted": self.trusted}


_states: dict[str, KeyState] = {}
_ring: deque[Observation] = deque(maxlen=SHADOW_RING_SIZE)
_loaded = False


def _ledger_path() -> Path:
    return Path(SHADOW_LEDGER_FILE)


# --- Persistence --------------------------------------------------------------


def _append(observation: Observation) -> None:
    """Write one observation. Never raises — losing the record must not fail a
    request that was answered correctly."""
    try:
        path = _ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(observation.to_dict(), ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not break the caller
        logger.warning("Could not append to the shadow ledger: %s", exc)


def load_from_disk() -> None:
    """Rebuild the key states from the ledger, once, at startup.

    Without this a restart forgets every quarantine, and a key that was proven
    to merge wrongly goes straight back into service. The states are DERIVED
    from the observations rather than stored separately, so there is no second
    file that can disagree with the first.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True

    path = _ledger_path()
    if not path.exists():
        return

    try:
        with path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read the shadow ledger at %s: %s", path, exc)
        return

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            observation = Observation(**payload)
        except Exception:  # noqa: BLE001 — one bad line must not lose the rest
            continue
        _apply(observation)
        _ring.append(observation)


def _apply(observation: Observation) -> KeyState:
    """Fold one observation into its key's state."""
    state = _states.get(observation.key)
    if state is None:
        state = KeyState(key=observation.key, first_seen=observation.ts)
        _states[observation.key] = state

    state.last_seen = observation.ts
    if observation.verdict == VERDICT_CONFIRMED:
        state.confirmations += 1
    else:
        state.divergences += 1
        state.quarantined = True
        if not state.reason:
            state.reason = REASON_SHAPE
    return state


# --- The API the pipeline will use --------------------------------------------


def should_verify(key: str) -> bool:
    """Whether this hit must be shadowed rather than trusted.

    True for an unknown key, a key still short of its confirmations, and any key
    at all when the ledger is unavailable. False only for a key that has agreed
    with the full pipeline enough times.

    A quarantined key returns True as well, but callers should be checking
    `is_quarantined` first and not reading the cache at all.
    """
    if not SHADOW_VERIFY_ENABLED:
        return False
    load_from_disk()
    state = _states.get(key)
    return state is None or not state.trusted


def is_quarantined(key: str) -> bool:
    """Whether this key has been proven to merge questions it should not."""
    load_from_disk()
    state = _states.get(key)
    return bool(state and state.quarantined)


def record(
    key: str,
    question: str,
    adapted_sql: str,
    reference_sql: str,
    missing_operators: list[str] | None = None,
) -> Observation:
    """Compare an adapted plan against the reference and file the result.

    `missing_operators` comes from services/operators.missing_evidence — an
    operator the question carried that never reached the SQL. It is treated as a
    divergence in its own right, because it is a plan-level failure the skeleton
    comparison can miss: restoring `<` where the question said `بعد` produces a
    perfectly well-shaped query that answers the opposite of what was asked, and
    two such queries have identical skeletons apart from the comparator.
    """
    comparison = plan_shape.compare(adapted_sql, reference_sql)
    differences = list(comparison.differences)

    if missing_operators:
        differences.append(f"missing operator evidence: {', '.join(missing_operators)}")

    verdict = VERDICT_CONFIRMED if comparison.matches and not missing_operators else VERDICT_DIVERGED

    observation = Observation(
        ts=time.time(),
        key=key,
        question=question,
        verdict=verdict,
        adapted_sql=adapted_sql,
        reference_sql=reference_sql,
        differences=differences,
    )

    load_from_disk()
    state = _apply(observation)
    if missing_operators and state.quarantined and state.reason == REASON_SHAPE:
        state.reason = REASON_OPERATOR

    _ring.append(observation)
    _append(observation)

    if verdict == VERDICT_DIVERGED:
        logger.warning(
            "Shadow verification quarantined key %s on %r: %s",
            key[:12],
            question,
            "; ".join(differences) or "no detail",
        )

    return observation


# --- Reporting ----------------------------------------------------------------


def state_for(key: str) -> KeyState | None:
    load_from_disk()
    return _states.get(key)


def stats() -> dict:
    load_from_disk()
    states = list(_states.values())
    return {
        "enabled": SHADOW_VERIFY_ENABLED,
        "confirmations_required": SHADOW_CONFIRMATIONS,
        "keys": len(states),
        "trusted": sum(1 for state in states if state.trusted),
        "verifying": sum(1 for state in states if not state.trusted and not state.quarantined),
        "quarantined": sum(1 for state in states if state.quarantined),
        "observations": len(_ring),
    }


def recent(limit: int = 100, verdict: str | None = None) -> list[dict]:
    """Most recent observations first, optionally filtered to divergences."""
    load_from_disk()
    observations = [o for o in _ring if verdict is None or o.verdict == verdict]
    observations.reverse()
    return [observation.to_dict() for observation in observations[:limit]]


def quarantined_keys() -> list[dict]:
    """Every key proven unsafe, newest first. The list worth reading."""
    load_from_disk()
    states = [state for state in _states.values() if state.quarantined]
    states.sort(key=lambda state: state.last_seen, reverse=True)
    return [state.to_dict() for state in states]


def reset_for_tests() -> None:
    global _loaded
    _states.clear()
    _ring.clear()
    _loaded = False
