#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
DEMO_DIR="/private/tmp/sqlite-reconcile-demo"
FORCE=0

usage() {
    cat <<'EOF'
Usage:
  demo/setup_demo_repo.sh [--force] [demo-dir]

Creates a separate Git repository for the sqlite-reconcile presentation demo.

Options:
  --force    Remove the existing demo directory before recreating it.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --force)
            FORCE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            DEMO_DIR="$1"
            shift
            ;;
    esac
done

VENV_PYTHON="$REPO_ROOT/venv/bin/python"
MERGETOOL_ENTRY="$REPO_ROOT/src/sqlite-reconcile-mergetool"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "error: missing virtualenv Python at $VENV_PYTHON"
    echo "Run ./setup.sh from the project root first."
    exit 1
fi

if [ ! -x "$MERGETOOL_ENTRY" ]; then
    chmod +x "$MERGETOOL_ENTRY"
fi

if [ -e "$DEMO_DIR" ]; then
    if [ "$FORCE" -eq 1 ]; then
        rm -rf "$DEMO_DIR"
    else
        echo "error: demo directory already exists: $DEMO_DIR"
        echo "Use --force to recreate it."
        exit 1
    fi
fi

mkdir -p "$DEMO_DIR"
cd "$DEMO_DIR"

git init -q
git config user.email demo@example.com
git config user.name Demo

printf '*.db binary\n' > .gitattributes

git config mergetool.sqlite-reconcile.cmd \
    "\"$VENV_PYTHON\" \"$MERGETOOL_ENTRY\" \"\$BASE\" \"\$LOCAL\" \"\$REMOTE\" \"\$MERGED\""
git config mergetool.sqlite-reconcile.trustExitCode true
git config mergetool.prompt false
git config mergetool.keepBackup false
git config alias.sqlite-reconcile "mergetool --tool=sqlite-reconcile -- '*.db' '*.sqlite'"

cp "$REPO_ROOT/demo/check_state.sql" check_state.sql

PYTHONPATH="$REPO_ROOT/src" "$VENV_PYTHON" - <<'PY'
import sqlite3

from sqlite_wrapper import SQLiteWrapper

with sqlite3.connect("app.db") as con:
    con.executescript("""
    PRAGMA foreign_keys=ON;

    CREATE TABLE users (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      email TEXT UNIQUE NOT NULL,
      token TEXT
    );

    CREATE TABLE audit (
      id INTEGER PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id),
      message TEXT NOT NULL
    );

    CREATE TABLE accounts (
      id INTEGER PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id),
      balance INTEGER NOT NULL CHECK (balance >= 0)
    );

    CREATE TABLE settings (
      id INTEGER PRIMARY KEY,
      value TEXT NOT NULL
    );
    """)

with SQLiteWrapper("app.db") as db:
    db.execute("BEGIN")
    db.execute("INSERT INTO users(id, name, email, token) VALUES (1, 'Alice', 'alice@example.com', 'base-token-1')")
    db.execute("INSERT INTO users(id, name, email, token) VALUES (2, 'Bob', 'bob@example.com', 'base-token-2')")
    db.execute("INSERT INTO audit(id, user_id, message) VALUES (1, 1, 'base audit message')")
    db.execute("INSERT INTO accounts(id, user_id, balance) VALUES (1, 1, 100)")
    for i in range(1, 6):
        db.execute(f"INSERT INTO settings(id, value) VALUES ({i}, 'base-{i}')")
    db.execute("COMMIT")
PY

git add .gitattributes app.db check_state.sql
git commit -q -m "base"

echo
echo "Base database state:"
sqlite3 app.db < check_state.sql

git branch local
git branch remote

git checkout -q local
PYTHONPATH="$REPO_ROOT/src" "$VENV_PYTHON" - <<'PY'
from sqlite_wrapper import SQLiteWrapper

with SQLiteWrapper("app.db") as db:
    # L1: branch-local unsafe nondeterminism.
    db.execute("BEGIN")
    db.execute("UPDATE users SET token = random() WHERE id IN (1, 2)")
    db.execute("COMMIT")

    # L2: write-read writer, multi-statement transaction.
    db.execute("BEGIN")
    db.execute("UPDATE users SET name = 'Alice Local' WHERE id = 1")
    db.execute("INSERT INTO audit(id, user_id, message) VALUES (10, 1, 'local changed user name')")
    db.execute("COMMIT")

    # L3: integrity conflict with R2, multi-statement transaction.
    db.execute("BEGIN")
    db.execute("INSERT INTO users(id, name, email, token) VALUES (3, 'Carol', 'shared@example.com', 'local-carol')")
    db.execute("UPDATE settings SET value = 'local inserted Carol' WHERE id = 3")
    db.execute("COMMIT")

    # L4: write-write conflict with R3.
    db.execute("BEGIN")
    db.execute("UPDATE accounts SET balance = balance + 50 WHERE id = 1")
    db.execute("COMMIT")
PY

git add app.db
git commit -q -m "local changes"

git checkout -q remote
PYTHONPATH="$REPO_ROOT/src" "$VENV_PYTHON" - <<'PY'
from sqlite_wrapper import SQLiteWrapper

with SQLiteWrapper("app.db") as db:
    # R1: independent clean transaction.
    db.execute("BEGIN")
    db.execute("UPDATE settings SET value = 'remote independent' WHERE id = 5")
    db.execute("COMMIT")

    # R2: integrity conflict with L3.
    db.execute("BEGIN")
    db.execute("INSERT INTO users(id, name, email, token) VALUES (4, 'Dave', 'shared@example.com', 'remote-dave')")
    db.execute("COMMIT")

    # R3: write-write conflict with L4, multi-statement transaction.
    db.execute("BEGIN")
    db.execute("UPDATE accounts SET balance = balance - 20 WHERE id = 1")
    db.execute("INSERT INTO audit(id, user_id, message) VALUES (11, 1, 'remote adjusted balance')")
    db.execute("COMMIT")

    # R4: write-read reader affected by L2.
    db.execute("BEGIN")
    db.execute("UPDATE audit SET message = (SELECT name FROM users WHERE id = 1) WHERE id = 1")
    db.execute("COMMIT")
PY

git add app.db
git commit -q -m "remote changes"
git checkout -q local

cat <<EOF
Demo repository created at:
  $DEMO_DIR

Run the demo:
  cd "$DEMO_DIR"
  git merge remote || git sqlite-reconcile

Suggested resolutions are in:
  $REPO_ROOT/demo/README.md

Check the current database state at any point with:
  sqlite3 app.db < check_state.sql
EOF
