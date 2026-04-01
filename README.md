# sqlite-reconcile

A Git custom merge driver for SQLite database files. Automatically merges compatible SQLite database changes with intelligent conflict detection and resolution.

## Features

- **Automatic 3-way merging** — Intelligently merges INSERT/UPDATE/DELETE operations from base, local, and remote versions
- **Zero false negatives** — Conservative conflict detection prioritizes data integrity over merge success
- **Update-delete conflict detection** — Explicitly detects when one branch updates a row another branch deletes
- **Post-merge validation** — Runs PRAGMA foreign_key_check and PRAGMA integrity_check to ensure database consistency
- **Interactive conflict resolution** — GUI for manual resolution of unresolved conflicts (coming soon)

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

### 2. Configure your repository

In your Git repository where you have .db or .sqlite files:

```bash
# 1. Add .gitattributes to specify which files use the merge driver
# echo "*.db merge=sqlite" >> .gitattributes
echo "*.sqlite merge=sqlite" >> .gitattributes
git add .gitattributes

# 2. Configure git to use the driver
DRIVER_PATH="/path/to/sqlite-reconcile"
git config merge.sqlite.name "SQLite merge driver"
git config merge.sqlite.driver "$DRIVER_PATH/venv/bin/python $DRIVER_PATH/src/sqlite-reconcile %O %A %B %L %P"

# 3. Commit
git commit -m "Add SQLite merge driver configuration"
```

## Usage

Once configured, SQLite merges happen automatically

Exit codes:
- 0 = successful merge
- 1 = unresolved conflicts

## Project Roadmap

### MVP — Minimum Viable Product (March 13, 2026)

The smallest working version that solves the core problem:

- **Core 3-way merge algorithm** — Intelligently merges INSERT/UPDATE/DELETE operations from base/local/remote
- **Update-delete conflict detection** — Explicitly catches when one branch updates a row another deletes
- **Post-merge validation** — Runs PRAGMA checks to prevent corrupted/invalid databases
- **Manual conflict resolution** — Users can edit conflicts and resolve them

After MVP, the tool is **usable but basic**—automatic merges work, conflicts are caught, but limited flexibility.

### Priority 1-4 — Advanced Features (April-June 2026)

See project plan for:
- **Priority 1**: Schema change detection, type mismatches, foreign key tracking
- **Priority 2**: Configurable resolution strategies, delta resolution
- **Priority 3**: Advanced GUI, visualization, bulk operations
- **Priority 4**: Semantic grouping, custom diff driver

## Limitations

- Unkeyed tables are skipped
- Cascading deletes treated as independent operations
- Tested on <100MB files

## License

MIT 

