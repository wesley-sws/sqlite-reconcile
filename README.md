# sqlite-reconcile

A Git custom merge driver for SQLite database files. Automatically merges compatible SQLite database changes with intelligent conflict detection and resolution.

## Features

- **Automatic 3-way merging** — Intelligently merges INSERT/UPDATE/DELETE operations from base, local, and remote versions
- **Zero false negatives** — Conservative conflict detection prioritizes data integrity over merge success
- **Update-delete conflict detection** — Explicitly detects when one branch updates a row another branch deletes
- **Conflict report output** — Writes machine-readable conflict details for unresolved cases
- **Git merge-driver integration** — Uses `%O %A %B %L %P` merge-driver contract correctly

## Installation

### 1. Clone and setup sqlite-reconcile

```bash
git clone https://github.com/yourusername/sqlite-reconcile.git
cd sqlite-reconcile
./setup.sh
```

If your system package manager installs `sqlite3` but not `sqldiff`, `setup.sh` automatically builds a local fallback at `tools/bin/sqldiff`. No shell profile (`.zshrc`) update is required for the merge driver.

Requirements:
- Python 3.10 or higher
- SQLite 3.x

### 2. Configure the merge driver

`setup.sh` configures the merge driver for this checkout, installs dependencies, and exposes `sqldiff`. That is enough for development and testing inside this repository.

If you want to use the driver in another repository, copy the same Git config into that repo and add `merge=sqlite-reconcile` to the matching `.gitattributes` entries there.

## Usage

Once configured, SQLite merges happen automatically

Exit codes:
- 0 = successful merge
- 1 = unresolved conflicts

## Conflict Detection Model

The merge driver starts from two diffs against a common base:

- `base -> ours`
- `base -> theirs`

It then compares those diffs using the relevant key for the table. For primary-key tables (and we don't consider tables without primary key in sqlite-reconcile), the primary key identifies the row. For unique-index handling, the unique columns identify the value set that must remain unique. For foreign-key checks, direct parent-child references and end-state FK validity are the things that must remain consistent.

### Primary Key

Primary-key conflicts are about two branches modifying the same logical row.

- Insert-Insert: both branches inserted the same primary key, but the row contents differ.
- Update-Update: both branches updated the same primary key, but the updated values differ.
- Update-Delete: one branch updated a row and the other deleted that same row.
- Delete-Delete: both branches removed the same row, so this is usually safe.

SQLite has an important exception here: on ordinary rowid tables, composite `PRIMARY KEY` columns can still contain `NULL` unless the schema also declares `NOT NULL` or the table is `WITHOUT ROWID` / `STRICT`. `sqlite-reconcile` currently treats primary keys as stable row identifiers and does not special-case `NULL` primary-key parts yet.

Primary-key changes are conservative by design. If `sqldiff` represents a primary-key edit as `DELETE` + `INSERT`, the merge driver treats that as two operations instead of guessing that it was a rename.

### Unique Indices

Unique-index conflicts are about duplicate values, not row identity.

Two branches can touch different rows and still collide if the merged result would violate a unique index.

- Insert-Insert: both branches create rows that want the same unique value.
- Insert-Update: one branch inserts a unique value while the other branch updates a different row to that same value.
- Update-Update: both branches change different rows toward the same unique value.

SQLite allows multiple `NULL` values in a `UNIQUE` index, and that behavior does not change just because a table is `STRICT`, `WITHOUT ROWID`, or has an `INTEGER PRIMARY KEY`. The rule only changes if the indexed columns are explicitly declared `NOT NULL`. In other words, `NULL` does not collide with `NULL` for uniqueness purposes.

Unlike primary-key conflicts, unique-index conflicts are often detected at merge-apply time as a constraint problem, because the conflict is caused by the final merged table state rather than by the same row identity on both sides.

### Foreign-Key Relationships

Foreign-key conflicts are about parent-child consistency across branches.

- Insert-Delete: one branch inserts a child row while the other deletes the parent row it depends on.
- Update-Delete: one branch updates a child row or foreign-key column while the other deletes the referenced parent row.
- Parent-key-Update vs Child-Reference: one branch changes a parent key while the other branch creates or updates child references to the old key.

The practical rule is simple: if the merged result would leave a child row pointing at a missing parent, that is a foreign-key conflict. Those cases are handled conservatively and then validated with foreign-key checks.

### Foreign-Key Policy (Conservative, Action-Agnostic)

`sqlite-reconcile` currently focuses foreign-key conflict detection on direct parent-child references plus final integrity validation.

- Pairwise parent-child FK checks are used for semantic conflict detection.
- `PRAGMA foreign_key_check` is used as the final guardrail on merged output.
- Multi-hop ancestor-descendant intent inference is not required for this FK-integrity objective.

Rationale:
- For FK-focused correctness, direct edge checks plus final FK validation are sufficient.
- This keeps behavior deterministic and easier to reason about.
- It avoids over-flagging FK-valid merges based on higher-level intent interpretation.

Why multi-hop is not needed for this objective:
- SQLite foreign-key constraints are defined and enforced on direct parent-child edges.
- Any true FK-integrity failure in deeper table chains still manifests as at least one broken direct edge, which `PRAGMA foreign_key_check` reports.
- Therefore, adding ancestor-descendant intent inference does not improve FK-integrity guarantees; it mostly adds policy-level opinion and extra false positives.

This policy is intentional: if the merged database is FK-valid, we treat it as acceptable from the FK perspective without extra multi-hop intent analysis.

### Design Choice 
- Avoid fuzzy semantic-update inference from similar row content
  - In the case of updating a row's primary key column only, sqldiff identifies such change as DELETE+INSERT operations separately, which is semantically equivalent to a single UPDATE operation on the primary key. In sqlite-reconcile we considered whether to group the DELETE+INSERT into UPDATE, which can be beneficial and aligning to the user's intent at times, but ultimately decided not to.
  - The argument to consider this change can be illustrated by the following example: consider a "movies" table with primary key column being "title", with the change base->ours being changing non-key columns of the row titled `"The Matrix"` (UPDATE), and the change from base->theirs being changing the title of that same row from `"The Matrix"` to `"Matrix"` (DELETE-INSERT from sqldiff)
    - Under our individual table conflict detection rules, we would flag an UPDATE-DELETE Conflict but the INSERT won't have a matching conflict hence will still be applied
    - In this case clearly that is not what we want, and indeed pairing the DELETE+INSERT into an indivisible single UPDATE operation would solve the problem, but under what condition do we pair the DELETE+INSERT into an UPDATE? 
    - A sensible approach would be to find DELETE_INSERT pairs from the outcome of sqldiff where row to be deleted has the same values as the row to be inserted for every column but the primary key column(s) - but how about unique indices? Consider a table with only two columns that make up a composite key: Using this approach would mean that every delete and insert statement generated from sqldiff can be paired into an update statement since there are no non-key columns, which is clearly a poor choice
    - Therefore it is more sensible to keep a simple, deterministic and conservative approach of keeping the DELETE+INSERT instead of guessing whether the user intended to update the row's primary key or if they actually deleted the row and added a new row that happend to have the same values in all non-key columns
  
## Current Status

### Implemented

- Primary-key-based semantic merge for row-level `INSERT` / `UPDATE` / `DELETE`
- Baseline unique-index conflict handling (including composite unique indexes, excluding partial/expression indexes)
- Conflict detection for incompatible row operations (including update-vs-delete)
- Deterministic merge output assembly from matched and non-conflicting changes
- JSON conflict report generation for unresolved conflicts
- End-to-end Git merge-driver wiring (`%O %A %B %L %P`)

### In Progress (Current Focus)
- **Foreign-key-aware conflict handling**
  - Goal: detect and handle parent-child cross-branch conflicts (for example child insert/update vs parent delete)
  - Direction: conservative, action-agnostic direct parent-child checking plus final `foreign_key_check`
  - Scope now: practical pairwise FK conflict cases and predictable diagnostics

## Roadmap

### Near Term

- Partial unique index handling (`WHERE`-filtered uniqueness)
- Expression unique index handling (for example `lower(email)`)
- Integrity checks as merge guardrails (`foreign_key_check`, `integrity_check`)
- Collation-aware uniqueness and value comparison semantics (later in this phase)
- Better diagnostics in conflict JSON output (more actionable conflict context)
- Schema guardrails (detect PK/schema drift early and fail clearly)

### Later
- Trigger/view/schema-object change detection with explicit manual-resolution flow
- Performance tuning and larger-scale test coverage

### Much Later / Nice-to-Have

- GUI conflict resolution workflow
- Semantic grouping / higher-level merge UX
- Rich visual diff tooling

## Limitations

- Unkeyed tables are skipped
- Cascading deletes treated as independent operations
- Semantic rename/move inference is intentionally conservative; ambiguous delete+insert similarity is not auto-merged into an inferred update
- Current unique-index checks assume each input snapshot already satisfies its own unique constraints; pre-existing unique violations are out of scope and can lead to undefined conflict classification
- Composite primary-key `NULL` edge cases are intentionally not special-cased yet
- Foreign-key conflict reporting is conservative and action-agnostic; FK actions are not currently used to suppress conflicts
- Foreign-key checking is pairwise plus final `foreign_key_check`; multi-hop intent inference is not required for this FK-integrity objective

## License

MIT 

