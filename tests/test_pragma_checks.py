import shutil
import sqlite3

import sqlglot


def _clone_three_way(base_path: str, ours_path: str, theirs_path: str) -> None:
    shutil.copy2(base_path, ours_path)
    shutil.copy2(base_path, theirs_path)


def _open_three_way(base_path: str, ours_path: str, theirs_path: str):
    base_con = sqlite3.connect(base_path)
    ours_con = sqlite3.connect(ours_path)
    theirs_con = sqlite3.connect(theirs_path)
    base_con.row_factory = sqlite3.Row
    ours_con.row_factory = sqlite3.Row
    theirs_con.row_factory = sqlite3.Row
    return base_con, ours_con, theirs_con


class TestPragmaChecks:
    def test_records_foreign_key_check_failures(self, tmp_path, merge_driver):
        base_path = tmp_path / "base.db"
        ours_path = tmp_path / "ours.db"
        theirs_path = tmp_path / "theirs.db"

        schema_sql = """
        CREATE TABLE parent (
            id INTEGER PRIMARY KEY
        );
        CREATE TABLE child (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER,
            FOREIGN KEY(parent_id) REFERENCES parent(id)
        );
        """

        with sqlite3.connect(base_path) as con:
            con.executescript(schema_sql)
            con.execute("INSERT INTO parent (id) VALUES (1)")
            con.execute("INSERT INTO child (id, parent_id) VALUES (1, 1)")
            con.commit()

        _clone_three_way(str(base_path), str(ours_path), str(theirs_path))

        # Create an FK-violating row in ours.
        with sqlite3.connect(ours_path) as con:
            con.execute("PRAGMA foreign_keys = OFF")
            con.execute("INSERT INTO child (id, parent_id) VALUES (2, 999)")
            con.commit()

        base_to_ours_parsed = [sqlglot.parse_one("INSERT INTO child (id, parent_id) VALUES (2, 999)")]
        base_to_theirs_parsed = []

        base_con, ours_con, theirs_con = _open_three_way(str(base_path), str(ours_path), str(theirs_path))
        try:
            _, invalid_tables = merge_driver.check_valid_tables(
                base_to_ours_parsed,
                base_to_theirs_parsed,
                base_con.cursor(),
                ours_con.cursor(),
                theirs_con.cursor(),
            )
        finally:
            base_con.close()
            ours_con.close()
            theirs_con.close()

        assert "child" in invalid_tables
        reasons = invalid_tables["child"].invalid_reasons
        assert any(
            reason["failure_type"] == "pragma_foreign_check_failed - child table"
            and reason["failed_branch"] == "ours"
            for reason in reasons
        )

    def test_records_integrity_check_failures(self, tmp_path, merge_driver):
        base_path = tmp_path / "base.db"
        ours_path = tmp_path / "ours.db"
        theirs_path = tmp_path / "theirs.db"

        schema_sql = """
        CREATE TABLE metrics (
            id INTEGER PRIMARY KEY,
            score INTEGER CHECK(score >= 0)
        );
        """

        with sqlite3.connect(base_path) as con:
            con.executescript(schema_sql)
            con.execute("INSERT INTO metrics (id, score) VALUES (1, 10)")
            con.commit()

        _clone_three_way(str(base_path), str(ours_path), str(theirs_path))

        # Insert an invalid CHECK value into ours by temporarily disabling CHECK enforcement.
        with sqlite3.connect(ours_path) as con:
            con.execute("PRAGMA ignore_check_constraints = ON")
            con.execute("INSERT INTO metrics (id, score) VALUES (2, -1)")
            con.commit()

        base_to_ours_parsed = [sqlglot.parse_one("INSERT INTO metrics (id, score) VALUES (2, -1)")]
        base_to_theirs_parsed = []

        base_con, ours_con, theirs_con = _open_three_way(str(base_path), str(ours_path), str(theirs_path))
        try:
            _, invalid_tables = merge_driver.check_valid_tables(
                base_to_ours_parsed,
                base_to_theirs_parsed,
                base_con.cursor(),
                ours_con.cursor(),
                theirs_con.cursor(),
            )
        finally:
            base_con.close()
            ours_con.close()
            theirs_con.close()

        assert "metrics" in invalid_tables
        reasons = invalid_tables["metrics"].invalid_reasons
        assert any(
            reason["failure_type"] == "pragma_integrity_check_failed"
            and reason["failed_branch"] == "ours"
            for reason in reasons
        )
