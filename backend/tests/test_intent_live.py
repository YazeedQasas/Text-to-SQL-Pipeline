"""The live gate on intent extraction: does the real model actually collide?

test_intent.py proves the rails hold given a model response. It cannot prove the
thing Stage 1 actually depends on, which is that the model returns the SAME
keyword for two questions of the same shape. That is a property of the model on
this hardware, so it needs the model.

SKIPPED BY DEFAULT. Needs LM Studio up with LLM_MODEL loaded, and is slow —
measured at 10-22s per extraction on the reference deployment, so a full run is
around ten minutes. Enable with:

    INTENT_LIVE_TESTS=1 pytest tests/test_intent_live.py -v

The pairs are the same ones the deterministic suite uses, so a failure here
against a pass there localises the fault to the model rather than the rails.

RECORDED RESULT (Gemma 4 E4B, 2026-08-11): 6/10 collision pairs with a
~700-token prompt, 5/10 with a ~200-token one. Both runs got 10/10 on role
tagging and 3/3 on keeping different shapes apart. Every failure was a MISSED
collision — two questions of one shape getting two keys — and never a wrong
merge. See the Stage 1 findings: free-text keyword generation was not stable
enough on this model to be a cache key, and this file is what measures that.
"""

import asyncio
import os

import httpx
import pytest

from app.config import LM_STUDIO_BASE_URL
from app.services import intent


def _lm_studio_is_up() -> bool:
    try:
        return httpx.get(f"{LM_STUDIO_BASE_URL}/models", timeout=3.0).status_code == 200
    except Exception:  # noqa: BLE001 — any failure means "not available"
        return False


pytestmark = pytest.mark.skipif(
    os.getenv("INTENT_LIVE_TESTS") != "1" or not _lm_studio_is_up(),
    reason="live extraction: set INTENT_LIVE_TESTS=1 with LM Studio running",
)


# (label, question A, question B) — same shape, one differing value.
COLLISION_PAIRS = [
    ("year", "كم عدد القضايا المسجلة في 2023؟", "كم عدد القضايا المسجلة في 2024؟"),
    ("year", "كم جلسة عُقدت في 2022؟", "كم جلسة عُقدت في 2025؟"),
    ("year", "ما هي القضايا الواردة في 2021؟", "ما هي القضايا الواردة في 2024؟"),
    ("role", "من هو المدعي؟", "من هو المدعى؟"),
    ("role", "قضايا المدعي", "قضايا المدعى"),
    ("role", "ما هي قضايا المستدعي؟", "ما هي قضايا المستدعى ضده؟"),
    (
        "role",
        "من هو المشتكي في القضية MS-2026-0301؟",
        "من هو المشتكى عليه في القضية MS-2026-0301؟",
    ),
    ("case", "ما هي جلسات القضية MS-2026-0301؟", "ما هي جلسات القضية MS-2026-0044؟"),
    ("court", "كم قضية في محكمة الصلح؟", "كم قضية في محكمة البداية؟"),
    ("name", "ما هي قضايا القاضي سامي الحديد؟", "ما هي قضايا القاضية إيمان مصطفى؟"),
]

DISTINCT_SHAPES = [
    ("كم عدد القضايا في 2024؟", "من هو قاضي القضية MS-2026-0301؟"),
    ("ما هي المحاكم الموجودة؟", "كم جلسة عُقدت في 2025؟"),
    ("ما هي القضايا المدورة؟", "من هو المدعي في القضية MS-2026-0301؟"),
]

ROLE_PAIRS = [pair for pair in COLLISION_PAIRS if pair[0] == "role"]


@pytest.mark.parametrize(("label", "first", "second"), COLLISION_PAIRS)
def test_same_shape_questions_share_a_key(label, first, second):
    """The gate. A failure here is a missed collision, not a wrong answer.

    Worth reading as a measurement rather than a pass/fail: this is the number
    that says whether an LLM-written keyword is stable enough to key a cache.
    """
    del label
    a, b = asyncio.run(intent.extract(first)), asyncio.run(intent.extract(second))

    assert a.cacheable and b.cacheable, f"{a.reason!r} / {b.reason!r}"
    assert intent.keyword_hash(a.keyword) == intent.keyword_hash(b.keyword), (
        f"{first!r} -> {a.keyword!r}   {second!r} -> {b.keyword!r}"
    )


@pytest.mark.parametrize(("label", "first", "second"), COLLISION_PAIRS)
def test_same_shape_questions_keep_different_specifics(label, first, second):
    """The half that makes a shared key survivable. This must never fail."""
    del label
    a, b = asyncio.run(intent.extract(first)), asyncio.run(intent.extract(second))

    assert a.specifics != b.specifics, (
        f"both questions reduced to the same specifics — whatever distinguishes "
        f"{first!r} from {second!r} has been lost entirely"
    )


@pytest.mark.parametrize(("label", "first", "second"), ROLE_PAIRS)
def test_party_roles_are_always_tagged(label, first, second):
    """The safety-critical one: an untagged role is unrecoverable.

    A failure is not a missed collision, it is the question being refused a key
    (rail 3) — correct but with the feature off. It must never instead come back
    cacheable with the role missing.
    """
    del label
    for question in (first, second):
        result = asyncio.run(intent.extract(question))
        roles = intent.role_terms_in(question)
        assert roles, f"the probe question {question!r} no longer names a role"
        if result.cacheable:
            assert result.values_by_tag("role") or result.values_by_tag("other"), (
                f"{question!r} got a key with no role specific — the plaintiff's "
                "plan can now be served for the defendant's question"
            )


@pytest.mark.parametrize(("first", "second"), DISTINCT_SHAPES)
def test_different_shapes_get_different_keys(first, second):
    a, b = asyncio.run(intent.extract(first)), asyncio.run(intent.extract(second))

    assert intent.keyword_hash(a.keyword) != intent.keyword_hash(b.keyword), (
        f"{first!r} and {second!r} both -> {a.keyword!r}"
    )
