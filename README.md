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

It then compares those diffs using the relevant key for the table. For primary-key tables (and we don't consider tables without primary key in sqlite-reconcile), the primary key identifies the row. For unique-index handling, the unique columns identify the value set that must remain unique. For foreign-key checks, the parent-child relationship is the thing that must remain consistent.

### Primary Key

Primary-key conflicts are about two branches modifying the same logical row.

- Insert-Insert: both branches inserted the same primary key, but the row contents differ.
- Update-Update: both branches updated the same primary key, but the updated values differ.
- Update-Delete: one branch updated a row and the other deleted that same row.
- Delete-Delete: both branches removed the same row, so this is usually safe.

Primary-key changes are conservative by design. If `sqldiff` represents a primary-key edit as `DELETE` + `INSERT`, the merge driver treats that as two operations instead of guessing that it was a rename.

### Unique Indices

Unique-index conflicts are about duplicate values, not row identity.

Two branches can touch different rows and still collide if the merged result would violate a unique index.

- Insert-Insert: both branches create rows that want the same unique value.
- Insert-Update: one branch inserts a unique value while the other branch updates a different row to that same value.
- Update-Update: both branches change different rows toward the same unique value.

Unlike primary-key conflicts, unique-index conflicts are often detected at merge-apply time as a constraint problem, because the conflict is caused by the final merged table state rather than by the same row identity on both sides.

### Foreign-Key Relationships

Foreign-key conflicts are about parent-child consistency across branches.

- Insert-Delete: one branch inserts a child row while the other deletes the parent row it depends on.
- Update-Delete: one branch updates a child row or foreign-key column while the other deletes the referenced parent row.

The practical rule is simple: if the merged result would leave a child row pointing at a missing parent, that is a foreign-key conflict. Those cases are handled conservatively and then validated with foreign-key checks.

### Design Choice 
- Avoid fuzzy semantic-update inference from similar row content
  - In the case of updating a row's primary key column only, sqldiff identifies such change as DELETE+INSERT operations separately, which is semantically equivalent to a single UPDATE operation on the primary key. In sqlite-reconcile we considered whether to group the DELETE+INSERT into UPDATE, which can be beneficial and aligning to the user's intent at times, but ultimately decided not to.
  - The argument to consider this change can be illustrated by the following example: consider a "movies" table with primary key column being "title", with the change base->ours being changing non-key columns of the row titled `"The Matrix"` (UPDATE), and the change from base->theirs being changing the title of that same row from `"The Matrix"` to `"Matrix"` (DELETE-INSERT from sqldiff)
    - Under our individual table conflict detection rules, we would flag an UPDATE-DELETE Conflict but the INSERT won't have a matching conflict hence will still be applied
    - In this case clearly that is not what we want, and indeed pairing the DELETE+INSERT into indivisible single UPDATE operation would solve the problem, but under what condition do we pair the DELETE+INSERT into an UPDATE? 
    - A sensible approach would be to find DELETE_INSERT pairs from the outcome of sqldiff where row to be deleted has the same values as the row to be inserted for every column but the primary key column(s) - but how about unique indices? And consider a table with only two columns that make up a composite key, using this approach would mean that every delete and insert statement generated from sqldiff can be paired into an update statement since there are no non-key columns
    - Therefore it is more sensible to keep a simple, deterministic and conservative approach of keeping the DELETE+INSERT instead of guessing whether the user intended to update the row's primary key or did they actually deleted the row and added a new row that happend to have the same values in all non-key columns
  
## Current Status

### Implemented

- Primary-key-based semantic merge for row-level `INSERT` / `UPDATE` / `DELETE`
- Conflict detection for incompatible row operations (including update-vs-delete)
- Deterministic merge output assembly from matched and non-conflicting changes
- JSON conflict report generation for unresolved conflicts
- End-to-end Git merge-driver wiring (`%O %A %B %L %P`)

### In Progress (Current Focus)
- **Unique index handling**
- **Foreign-key-aware conflict handling**
	- Goal: detect and handle parent-child cross-branch conflicts (for example child insert/update vs parent delete)
	- Direction: salvage-oriented strategy with deterministic policy and explicit rejected/accepted statement reporting
	- Scope now: focus on practical FK conflict cases first, advanced FK semantics later

## Roadmap

### Near Term

- Unique index conflict handling (baseline non-partial, non-expression indexes)
- Partial unique index handling (`WHERE`-filtered uniqueness)
- Expression unique index handling (for example `lower(email)`)
- SQLite `NULL` behavior under `UNIQUE` constraints (multiple `NULL`s allowed)
- Integrity checks as merge guardrails (`foreign_key_check`, `integrity_check`)
- Collation-aware uniqueness and value comparison semantics (later in this phase)
- Better diagnostics in conflict JSON output (more actionable conflict context)
- Schema guardrails (detect PK/schema drift early and fail clearly)

### Later

- FK action semantics (`CASCADE`, `SET NULL`, deferred constraints)
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
- Unique-constraint conflicts are handled conservatively and may surface as unresolved conflicts even when a manual rename would be safe
- Foreign-key validation is a post-merge guardrail, not a full semantic dependency solver
- Tested on <100MB files

## License

MIT 

