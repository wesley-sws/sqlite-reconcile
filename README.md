# sqlite-reconcile

`sqlite-reconcile` is an experimental Git mergetool for transaction-level
merging of SQLite database files. Instead of diffing raw database pages,
applications execute writes through a small SQLite wrapper. The wrapper logs
committed SQL transactions, and the terminal mergetool later uses those logs to
replay, check, and resolve changes from two Git branches.

This project was developed as part of a university final-year project.

## Setup

Run setup from the project root:

```sh
./setup.sh
```

This creates or reuses `venv`, installs Python dependencies, marks SQLite files
as binary in `.gitattributes`, and configures Git's mergetool interface plus a
DB-only Git alias:

```sh
git sqlite-reconcile
```

It also configures a convenience alias that runs `git merge` first and launches
the SQLite mergetool if Git stops with conflicts:

```sh
git merge-sqlite other-branch
```

The setup script intentionally configures a **Git mergetool**, not a merge
driver. Git first detects the SQLite file as conflicted, then the user invokes
the mergetool to resolve it. The tool is not made the default mergetool, because
Git does not choose mergetools by file extension.

The setup script configures Git for the repository where it is run. To use
`sqlite-reconcile` from another repository, configure that repository to point at
this checkout as shown in the usage section below.

## Required Logging Model

Database edits must be made through `src/sqlite_wrapper/wrapper.py`. The wrapper
creates two internal tables:

- `_sqlite_merge_transactions`
- `_sqlite_merge_log`

Each committed transaction receives a transaction record, and each logged DML
statement is stored with the SQL text used for replay. If a database is edited
directly with `sqlite3` or another unwrapped connection, the mergetool cannot
know what changed.

DDL and schema changes are currently outside the supported merge scope.

Minimal wrapper usage, assuming this repository's `src` directory is on
`PYTHONPATH`:

```python
from sqlite_wrapper import SQLiteWrapper

with SQLiteWrapper("app.db") as db:
    db.execute("BEGIN")
    db.execute("UPDATE users SET name = 'Alice' WHERE id = 1")
    db.execute("COMMIT")
```

## Usage

In a repository containing logged SQLite databases, make sure the relevant files
are treated as binary by Git. The `.db` extension is only a convention here;
the mergetool still validates that inputs are readable SQLite databases with the
wrapper log tables before merging.

```gitattributes
*.db binary
*.sqlite binary
```

When Git reports a conflict on a logged SQLite database, run:

```sh
git sqlite-reconcile
```

The mergetool receives Git's base, local, remote, and merged files, loads the
logged transactions from both branches, and writes the resolved database back to
the merged path.

To resolve one database explicitly, pass its path:

```sh
git mergetool --tool=sqlite-reconcile -- path/to/database.db
```

If using the tool from another repository, configure that repository's Git
mergetool command to point at this checkout:

```sh
TOOL_HOME=/absolute/path/to/sqlite-reconcile
git config mergetool.sqlite-reconcile.cmd "\"$TOOL_HOME/venv/bin/python\" \"$TOOL_HOME/src/sqlite-reconcile-mergetool\" \"\$BASE\" \"\$LOCAL\" \"\$REMOTE\" \"\$MERGED\""
git config mergetool.sqlite-reconcile.trustExitCode true
git config mergetool.prompt false
git config mergetool.keepBackup false
git config alias.sqlite-reconcile "mergetool --tool=sqlite-reconcile -- '*.db' '*.sqlite'"
git config alias.merge-sqlite '!f() { git merge "$@" || git sqlite-reconcile; }; f'
```

The Git config options mean:

- `mergetool.sqlite-reconcile.trustExitCode=true`: Git trusts the tool's exit
  status. Exit code `0` means the database was resolved; nonzero means it was
  not.
- `mergetool.prompt=false`: Git does not ask for confirmation before launching
  the mergetool for each selected file.
- `mergetool.keepBackup=false`: Git does not keep `.orig` backup files such as
  `app.db.orig` after the mergetool succeeds.

A typical merge then looks like:

```sh
git merge other-branch
git sqlite-reconcile
git status
git add path/to/database.db
git commit
```

Or, using the convenience alias:

```sh
git merge-sqlite other-branch
git status
git add path/to/database.db
git commit
```

If all unresolved conflicts are logged SQLite database files, this is also
equivalent:

```sh
git mergetool --tool=sqlite-reconcile -- '*.db' '*.sqlite'
```

If the merge also contains non-SQLite conflicts, keep the path filter or pass the
SQLite database paths explicitly so this mergetool is only run on files it
understands. The CLI also rejects non-`.db`/`.sqlite` targets as a safety net.

## How the Merge Works

At a high level:

1. The base, local, and remote databases are loaded.
2. The mergetool reads logged transactions after the base transaction watermark.
3. Transactions are considered in a fixed replay order:
   local `X1`, remote `Y1`, local `X2`, remote `Y2`, and so on.
4. Before accepting the current transaction, the tool checks it against the
   remaining transactions from the opposite branch.
5. Static metadata is used as a cheap filter for possible write-write,
   write-read, integrity, and omitted integer primary-key conflicts.
6. When needed, execution-based probes run against a main database and an
   attached control database to reduce false positives.
7. If a conflict is found, the terminal UI lets the user edit or delete
   statements/transactions, or accept reviewable conflicts.

The implementation uses `sqlglot` to parse supported DML statements and to build
metadata such as written tables, referenced columns, key columns, and cascade
effects.

## Supported Conflict Categories

The tool currently models:

- write-write conflicts between overlapping writes
- ordered write-read conflicts where the current transaction changes a later
  transaction's read behavior
- integrity conflicts involving primary keys, unique constraints, and foreign
  keys
- omitted `INTEGER PRIMARY KEY` replay issues
- reviewable SQLite conflict-resolution behavior such as `OR IGNORE` and
  `OR REPLACE`
- conservative static handling for foreign-key cascade effects

## Limitations

The project focuses on logged DML transactions over a stable persistent schema.
The following are outside the supported correctness scope or are handled
conservatively:

- DDL and schema changes
- temporary tables
- views unless expanded
- triggers and hidden trigger side effects
- external attached databases
- unsupported SQLite syntax or parser gaps
- UPSERT as a distinct automatic merge behavior
- precise execution-based refinement for cascade-hidden row effects

When unsupported behavior is detected, the tool should block automatic merge or
ask for user resolution rather than silently claiming a precise merge.

## Development

Run the test suite:

```sh
venv/bin/pytest -q
```

Run type checking:

```sh
venv/bin/pyright src tests
```

Useful entry points:

- `src/sqlite_wrapper/wrapper.py`: logging wrapper around `sqlite3`
- `src/sqlite-reconcile-mergetool`: terminal mergetool entry point
- `src/merge/terminal_mergetool.py`: top-level merge loop
- `src/merge/sql_metadata.py`: statement and transaction metadata extraction
- `src/merge/static_analysis.py`: static conflict filtering
- `src/merge/remaining_execution.py`: execution-based suffix checks
- `src/merge/control_db.py`: attached control database and SQL rewriting
- `src/sqlite_conflict_resolution.py`: SQLite conflict-resolution compatibility helpers

## Use of AI Assistance

Generative AI tools, including ChatGPT-5 and Codex by OpenAI, were used during
development as interactive assistants for design discussion, debugging,
implementation suggestions, code review, test-case ideas, synthetic evaluation
workload ideas, and documentation support.

AI outputs were not treated as authoritative. Code suggestions were integrated
iteratively and checked through the project test suite, manual inspection of key
paths, and comparison with expected behaviour. Evaluation results reported for the
project were produced by running the implementation and evaluation scripts, not by
AI-generated values.

AI support was used more heavily for syntax-heavy and repetitive work, especially
the SQLite conflict-resolution compatibility helpers in
`src/sqlite_conflict_resolution.py`, test scaffolding, evaluation-support scripts,
and maintenance tasks such as updating this README and setup script. These parts
remain part of the submitted implementation and are covered by the same tests and
evaluation checks as the rest of the project.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
