# Legal concepts — authoring guide

The concepts themselves live in `concepts.json` next to this file. JSON has no
comments, so everything that used to be prose at the top of the old
`ingestion/concepts.py` lives here instead.

## Why this file exists

A judge asks in the vocabulary of the Palestinian court system. That vocabulary
is largely absent from the database: nothing anywhere stores the string
"محكمة الصلح", and no column marks a case as "متقادمة". The schema documents
describe what the columns ARE; concepts describe what the user's words MEAN in
terms of those columns.

Each concept is embedded and stored as one Qdrant point in its own collection
(`legal_concepts`). At query time the user's question is searched against it, and
any concept that clears the score threshold is added to the SQL prompt as a
glossary line.

## Field contract

```json
{
  "id": "sulh_court",
  "term": "محكمة الصلح",
  "aliases": ["محاكم الصلح", "الصلح"],
  "definition": "…",
  "sql": "courts.level = 'ابتدائية'",
  "tables": ["courts"]
}
```

- `id` is the identity of the concept and must never change. Point IDs are
  `uuid5(namespace, "concept:" + id)`, so editing `id` orphans the old point and
  creates a second one. Fix typos in `term`, never in `id`.
- `term` / `aliases` / `definition` are the ONLY fields that get embedded. They
  are what an Arabic question is matched against, so write them the way a judge
  or clerk would actually phrase things — not the way a database designer would.
- `sql` is NEVER embedded. Putting SQL in the embedded blob drags the vector away
  from the Arabic question it needs to match. It rides in the payload and is
  rendered into the prompt separately.
- `sql` is a FRAGMENT, never a complete query — a WHERE predicate, a computed
  expression, or a join path. A whole SELECT would be copied verbatim and the
  rest of the user's question ignored. Always write identifiers fully qualified
  (`courts.level`, not `level`) so the fragment names its own tables.
- `tables` does double duty: it biases schema retrieval toward the tables the
  concept needs, and it tells the model which tables a multi-table fragment
  spans. Every table named here must exist in the indexed schema docs.

## How it syncs

`concepts.json` and the `legal_concepts` Qdrant collection are kept in step by
`backend/app/services/concept_sync.py`, which also keeps a snapshot in
`.concepts-sync-state.json`. Do not edit the snapshot — it is what lets the
sync tell "you deleted this from the file" apart from "this was just added to
Qdrant". Delete it and the next sync treats both sides as new and merges them.

Edit `concepts.json` and the sync picks it up. Delete a concept from it and the
point is removed from Qdrant. Delete a point from Qdrant (dashboard, API,
whatever) and it is removed from `concepts.json`.

A sync that would delete more than `CONCEPT_SYNC_DELETE_RATIO` of either side
refuses to run and raises a warning in the Activity tab instead. That is the
guard against an empty Qdrant volume wiping this file.

## REVIEW REQUIRED

These entries are a starting set drafted from general knowledge of Palestinian
legal terminology. They need review by someone who practises in these courts
before the system is put in front of real users, because a wrong mapping here
produces a confident, plausible, wrong answer rather than an error.

Two entries carry a judgement call rather than a fact:

- `taqadum` — the limitation period is a policy number, not a legal constant.
- `qadaya_basita` — the word has two readings and only one is mappable here.

Congestion concepts deliberately compute NOTHING. `db/05_congestion.sql` already
classifies pending cases against per-category thresholds held in
`congestion_rules`; those concepts only name the values it produces. Never
reimplement that policy in a `sql` fragment — it would silently compete with the
real one.

Note also that this demo database models a generic court system, not the
Palestinian one — it has no محاكم شرعية and no دائرة تنفيذ. Concepts that would
need those are deliberately absent rather than mapped onto something
approximate.

## BLOCKED ON the schema docs

`case_congestion`, `congestion_rules`, and the `cases.case_category` column are
NOT yet described in the indexed schema docs, so schema retrieval cannot surface
them. Until they are, the congestion and category concepts hand the model a
fragment referencing tables it cannot see, and it invents something instead.
