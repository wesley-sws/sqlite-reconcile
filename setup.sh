#!/usr/bin/env bash

set -euo pipefail

# Setup script for sqlite-reconcile's transaction-log terminal mergetool.
# This intentionally configures Git's mergetool interface, not a merge driver.

REPO_ROOT=$(git rev-parse --show-toplevel)
VENV_DIR="$REPO_ROOT/venv"
VENV_PYTHON="$VENV_DIR/bin/python"
MERGETOOL_ENTRY="$REPO_ROOT/src/sqlite-reconcile-mergetool"
ATTRIBUTES_FILE="$REPO_ROOT/.gitattributes"
MIN_SQLITE_VERSION="3.35.0"

echo "Setting up sqlite-reconcile terminal mergetool..."

has_command() {
    command -v "$1" >/dev/null 2>&1
}

ensure_line_in_file() {
    local line="$1"
    local file="$2"

    touch "$file"
    if ! grep -Fqx -- "$line" "$file"; then
        printf '%s\n' "$line" >> "$file"
    fi
}

require_python() {
    if ! has_command python3; then
        echo "error: python3 not found. Please install Python 3.10+."
        exit 1
    fi

    if ! python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
    then
        echo "error: Python 3.10+ required, found $(python3 --version 2>&1)."
        exit 1
    fi

    echo "ok: found $(python3 --version 2>&1)"
}

ensure_venv() {
    if [ ! -x "$VENV_PYTHON" ]; then
        echo "Creating virtual environment at $VENV_DIR..."
        python3 -m venv "$VENV_DIR"
    fi

    echo "Installing Python dependencies..."
    "$VENV_PYTHON" -m pip install -q -r "$REPO_ROOT/requirements.txt"
}

check_python_sqlite() {
    "$VENV_PYTHON" - "$MIN_SQLITE_VERSION" <<'PY'
import sqlite3
import sys

required_text = sys.argv[1]
required = tuple(int(part) for part in required_text.split("."))
found_text = sqlite3.sqlite_version
found = tuple(int(part) for part in found_text.split(".")[:3])

if found < required:
    print(
        f"error: Python sqlite3 is linked against SQLite {found_text}; "
        f"{required_text}+ is required."
    )
    raise SystemExit(1)

print(f"ok: Python sqlite3 uses SQLite {found_text}")
PY
}

configure_attributes() {
    ensure_line_in_file "*.db binary" "$ATTRIBUTES_FILE"
    ensure_line_in_file "*.sqlite binary" "$ATTRIBUTES_FILE"
    echo "ok: SQLite database files are marked binary in .gitattributes"
}

configure_git_mergetool() {
    chmod +x "$REPO_ROOT/src/sqlite-reconcile" "$MERGETOOL_ENTRY"

    git config mergetool.prompt false
    git config mergetool.keepBackup false
    git config mergetool.sqlite-reconcile.trustExitCode true
    git config mergetool.sqlite-reconcile.cmd \
        "\"$VENV_PYTHON\" \"$MERGETOOL_ENTRY\" \"\$BASE\" \"\$LOCAL\" \"\$REMOTE\" \"\$MERGED\""
    git config alias.sqlite-reconcile \
        "mergetool --tool=sqlite-reconcile -- '*.db' '*.sqlite'"
    git config alias.merge-sqlite \
        '!f() { git merge "$@" || git sqlite-reconcile; }; f'

    echo "ok: Git mergetool 'sqlite-reconcile' configured for this checkout"
}

require_python
ensure_venv
check_python_sqlite
configure_attributes
configure_git_mergetool

echo
echo "Setup complete."
echo "Run a SQLite database merge with:"
echo "  git sqlite-reconcile"
echo "or run merge then launch the SQLite mergetool on conflicts with:"
echo "  git merge-sqlite other-branch"
echo "or:"
echo "  git mergetool --tool=sqlite-reconcile -- path/to/database.db"
