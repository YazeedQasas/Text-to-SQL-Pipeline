"""Tests for the three-way concept sync.

The truth table in concept_sync's docstring is the whole design, and it is the
thing that goes quietly wrong: a two-way diff cannot tell "removed from the
file" apart from "added to Qdrant", so an error here does not raise, it deletes.
Every row of that table gets a test.

The second half covers the delete rail, which exists for one specific accident —
Qdrant coming up on an empty volume and an unguarded reconcile emptying
concepts.json to match it.
"""

import pytest

from app.services.concept_docs import (
    ConceptDoc,
    build_concept_doc_text,
    concept_from_dict,
    concept_point_id,
    parse_concepts_file,
    same_content,
)
from app.services.concept_sync import (
    check_delete_rail,
    exceeds_delete_rail,
    plan_sync,
)


def concept(concept_id: str, definition: str = "تعريف", term: str = "مصطلح") -> ConceptDoc:
    return ConceptDoc(
        id=concept_id,
        term=term,
        definition=definition,
        sql="courts.level = 'ابتدائية'",
        tables=["courts"],
        aliases=["اسم آخر"],
    )


def as_map(*concepts: ConceptDoc) -> dict[str, ConceptDoc]:
    return {c.id: c for c in concepts}


# --- The truth table ----------------------------------------------------------


def test_new_in_file_is_pushed_to_qdrant():
    added = concept("sulh")

    plan = plan_sync([added], snapshot={}, qdrant_concepts={})

    assert [c.id for c in plan.upsert_to_qdrant] == ["sulh"]
    assert plan.delete_from_qdrant == []
    assert plan.removed_from_file == []
    assert [c.id for c in plan.file_contents] == ["sulh"]


def test_new_in_qdrant_is_added_to_the_file():
    existing = concept("bidaya")

    plan = plan_sync([], snapshot={}, qdrant_concepts=as_map(existing))

    assert plan.added_to_file == ["bidaya"]
    assert plan.upsert_to_qdrant == []
    assert plan.delete_from_qdrant == []
    assert [c.id for c in plan.file_contents] == ["bidaya"]


def test_removed_from_the_file_is_deleted_from_qdrant():
    gone = concept("taqadum")

    plan = plan_sync([], snapshot=as_map(gone), qdrant_concepts=as_map(gone))

    assert plan.delete_from_qdrant == ["taqadum"]
    assert plan.file_contents == []
    assert plan.removed_from_file == []


def test_removed_from_qdrant_is_removed_from_the_file():
    gone = concept("taqadum")

    plan = plan_sync([gone], snapshot=as_map(gone), qdrant_concepts={})

    assert plan.removed_from_file == ["taqadum"]
    assert plan.file_contents == []
    assert plan.delete_from_qdrant == []


def test_edited_in_the_file_is_pushed_to_qdrant():
    before = concept("sulh", definition="القديم")
    after = concept("sulh", definition="الجديد")

    plan = plan_sync([after], snapshot=as_map(before), qdrant_concepts=as_map(before))

    assert [c.definition for c in plan.upsert_to_qdrant] == ["الجديد"]
    assert [c.definition for c in plan.file_contents] == ["الجديد"]
    assert plan.conflicts == []


def test_edited_in_qdrant_is_written_back_to_the_file():
    before = concept("sulh", definition="القديم")
    after = concept("sulh", definition="الجديد")

    plan = plan_sync([before], snapshot=as_map(before), qdrant_concepts=as_map(after))

    assert plan.updated_in_file == ["sulh"]
    assert [c.definition for c in plan.file_contents] == ["الجديد"]
    assert plan.upsert_to_qdrant == []
    assert plan.conflicts == []


def test_edited_on_both_sides_keeps_the_file_and_reports_a_conflict():
    base = concept("sulh", definition="الأصل")
    file_version = concept("sulh", definition="نسخة الملف")
    qdrant_version = concept("sulh", definition="نسخة قدرانت")

    plan = plan_sync(
        [file_version], snapshot=as_map(base), qdrant_concepts=as_map(qdrant_version)
    )

    assert [c.definition for c in plan.file_contents] == ["نسخة الملف"]
    assert [c.definition for c in plan.upsert_to_qdrant] == ["نسخة الملف"]
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].concept_id == "sulh"
    # The losing version is carried on the conflict rather than dropped, so the
    # warning can show what was overwritten.
    assert plan.conflicts[0].qdrant_version["definition"] == "نسخة قدرانت"


def test_edited_identically_on_both_sides_is_not_a_conflict():
    base = concept("sulh", definition="الأصل")
    same = concept("sulh", definition="نفس التعديل")

    plan = plan_sync([same], snapshot=as_map(base), qdrant_concepts=as_map(same))

    assert plan.conflicts == []
    assert plan.is_noop()


def test_deleted_on_both_sides_is_a_noop():
    gone = concept("taqadum")

    plan = plan_sync([], snapshot=as_map(gone), qdrant_concepts={})

    assert plan.is_noop()
    assert plan.file_contents == []


def test_unchanged_everywhere_is_a_noop():
    unchanged = concept("sulh")

    plan = plan_sync(
        [unchanged], snapshot=as_map(unchanged), qdrant_concepts=as_map(unchanged)
    )

    assert plan.is_noop()
    assert [c.id for c in plan.file_contents] == ["sulh"]


# --- Missing snapshot ---------------------------------------------------------


def test_a_missing_snapshot_merges_rather_than_deletes():
    """Deleting the snapshot must never be a destructive act.

    With no record of a previous agreement, every concept looks new on whichever
    side it is on. The safe reading is "both were added", which merges — the
    worst case is a concept coming back, not one disappearing.
    """
    only_in_file = concept("in_file")
    only_in_qdrant = concept("in_qdrant")

    plan = plan_sync([only_in_file], snapshot={}, qdrant_concepts=as_map(only_in_qdrant))

    assert plan.delete_from_qdrant == []
    assert plan.removed_from_file == []
    assert sorted(c.id for c in plan.file_contents) == ["in_file", "in_qdrant"]
    assert [c.id for c in plan.upsert_to_qdrant] == ["in_file"]


def test_a_missing_snapshot_with_matching_sides_does_nothing():
    """The common case after deleting the snapshot: the two sides already agree."""
    shared = concept("sulh")

    plan = plan_sync([shared], snapshot={}, qdrant_concepts=as_map(shared))

    assert plan.is_noop()


def test_a_missing_snapshot_with_differing_sides_prefers_the_file():
    file_version = concept("sulh", definition="نسخة الملف")
    qdrant_version = concept("sulh", definition="نسخة قدرانت")

    plan = plan_sync([file_version], snapshot={}, qdrant_concepts=as_map(qdrant_version))

    assert [c.definition for c in plan.file_contents] == ["نسخة الملف"]
    assert len(plan.conflicts) == 1


# --- Ordering -----------------------------------------------------------------


def test_the_files_existing_order_is_preserved():
    """A sync must not reorder a file someone maintains by hand.

    Rewriting the order makes every sync show up as a whole-file change in their
    diff, which buries the one line that actually changed.
    """
    ordered = [concept("zebra"), concept("apple"), concept("mango")]
    snapshot = as_map(*ordered)

    plan = plan_sync(ordered, snapshot=snapshot, qdrant_concepts=snapshot)

    assert [c.id for c in plan.file_contents] == ["zebra", "apple", "mango"]


def test_concepts_adopted_from_qdrant_are_appended_not_interleaved():
    kept = concept("zebra")
    adopted = concept("apple")

    plan = plan_sync([kept], snapshot=as_map(kept), qdrant_concepts=as_map(kept, adopted))

    assert [c.id for c in plan.file_contents] == ["zebra", "apple"]


# --- The delete rail ----------------------------------------------------------


def test_the_rail_ignores_small_collections():
    """Removing 1 of 3 is 33% but obviously fine.

    A bare ratio would make a short glossary impossible to shrink, so the floor
    is what keeps small collections editable.
    """
    assert exceeds_delete_rail(delete_count=1, total=3) is False
    assert exceeds_delete_rail(delete_count=2, total=3) is False


def test_the_rail_trips_on_a_large_share_of_a_real_collection():
    # Default ratio is 0.30, so 6 of 19 is over and 5 is not.
    assert exceeds_delete_rail(delete_count=5, total=19) is False
    assert exceeds_delete_rail(delete_count=6, total=19) is True


def test_an_empty_qdrant_does_not_empty_the_file():
    """The accident this rail exists for.

    Qdrant restarts on a fresh volume, every concept looks deleted, and an
    unguarded reconcile would rewrite concepts.json to match.
    """
    concepts = [concept(f"c{i}") for i in range(19)]
    snapshot = as_map(*concepts)

    plan = plan_sync(concepts, snapshot=snapshot, qdrant_concepts={})

    assert len(plan.removed_from_file) == 19
    refusal = check_delete_rail(plan, file_total=19, qdrant_total=0)
    assert refusal != ""
    assert "concepts.json" in refusal


def test_an_empty_file_does_not_empty_qdrant():
    concepts = [concept(f"c{i}") for i in range(19)]
    snapshot = as_map(*concepts)

    plan = plan_sync([], snapshot=snapshot, qdrant_concepts=as_map(*concepts))

    assert len(plan.delete_from_qdrant) == 19
    refusal = check_delete_rail(plan, file_total=0, qdrant_total=19)
    assert refusal != ""
    assert "Qdrant" in refusal


def test_an_ordinary_deletion_is_not_refused():
    concepts = [concept(f"c{i}") for i in range(19)]
    snapshot = as_map(*concepts)

    plan = plan_sync(concepts[:-1], snapshot=snapshot, qdrant_concepts=as_map(*concepts))

    assert plan.delete_from_qdrant == ["c18"]
    assert check_delete_rail(plan, file_total=18, qdrant_total=19) == ""


# --- The record itself --------------------------------------------------------


def test_point_ids_are_stable_across_a_term_rename():
    """The id, not the term, is the identity.

    If a point id were derived from `term`, fixing a typo would orphan the old
    point and create a second one — and both would keep matching questions.
    """
    before = concept("sulh", term="محكمة الصلح")
    after = concept("sulh", term="محاكم الصلح")

    assert concept_point_id(before.id) == concept_point_id(after.id)
    assert concept_point_id("sulh") != concept_point_id("bidaya")


def test_the_embedded_text_excludes_sql():
    """`sql` must never reach the vector.

    The blob is matched against an Arabic question; identifier soup in it drags
    the vector away from the thing it exists to match.
    """
    text = build_concept_doc_text(concept("sulh"))

    assert "courts.level" not in text
    assert "مصطلح" in text
    assert "تعريف" in text


def test_content_equality_ignores_derived_fields():
    payload = concept("sulh").to_dict()
    payload["doc_text"] = "anything at all"

    assert same_content(concept_from_dict(payload), concept("sulh"))


def test_a_qdrant_payload_parses_the_same_as_a_file_entry():
    """Qdrant keys the concept as `concept_id`; the file uses `id`."""
    from_file = concept_from_dict({"id": "sulh", "term": "محكمة الصلح"})
    from_qdrant = concept_from_dict({"concept_id": "sulh", "term": "محكمة الصلح"})

    assert from_file.id == from_qdrant.id == "sulh"


def test_a_hand_written_string_where_a_list_belongs_is_accepted():
    """`"tables": "courts"` is the mistake people actually make in a JSON file.

    Failing the whole sync over one entry helps nobody.
    """
    parsed = concept_from_dict({"id": "sulh", "tables": "courts", "aliases": "الصلح"})

    assert parsed.tables == ["courts"]
    assert parsed.aliases == ["الصلح"]


def test_duplicate_ids_in_the_file_are_rejected():
    body = '{"concepts": [{"id": "sulh"}, {"id": "sulh"}]}'

    with pytest.raises(ValueError, match="Duplicate"):
        parse_concepts_file(body)


def test_a_bare_list_is_accepted_as_well_as_the_wrapper():
    """Someone assembling a file to upload will produce both shapes."""
    assert len(parse_concepts_file('[{"id": "a"}, {"id": "b"}]')) == 2
    assert len(parse_concepts_file('{"concepts": [{"id": "a"}]}')) == 1
