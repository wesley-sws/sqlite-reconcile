import sqlite3
import sys
from pathlib import Path

from sqlglot.errors import ParseError


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from merge import utils
from merge.utils import ALL_COLUMNS


def test_key_column_sets_extracts_partial_and_expression_unique_index_columns():
    with sqlite3.connect(":memory:") as con:
        con.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email TEXT,
                active INTEGER,
                tenant_id INTEGER,
                deleted_at TEXT
            );
            CREATE UNIQUE INDEX users_lower_email
                ON users(lower(email));
            CREATE UNIQUE INDEX users_active_email
                ON users(email)
                WHERE active = 1;
            CREATE UNIQUE INDEX users_tenant_lower_email
                ON users(tenant_id, lower(email))
                WHERE deleted_at IS NULL;
            """
        )

        key_sets = {
            frozenset(columns)
            for columns in utils.key_column_sets(con.cursor(), "users")
        }

    assert frozenset({"id"}) in key_sets
    assert frozenset({"email"}) in key_sets
    assert frozenset({"email", "active"}) in key_sets
    assert frozenset({"tenant_id", "email", "deleted_at"}) in key_sets


def test_key_column_sets_falls_back_for_unparseable_expression_index(monkeypatch):
    def fail_parse(*args, **kwargs):
        raise ParseError("synthetic parse failure")

    with sqlite3.connect(":memory:") as con:
        con.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email TEXT
            );
            CREATE UNIQUE INDEX users_lower_email
                ON users(lower(email));
            """
        )
        monkeypatch.setattr(utils, "parse_one", fail_parse)

        key_sets = utils.key_column_sets(con.cursor(), "users")

    assert {ALL_COLUMNS} in key_sets
