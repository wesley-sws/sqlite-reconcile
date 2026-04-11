import pytest
import sqlite3
import sqlglot


class TestConflictDetection:
    """Test the core conflict detection logic."""

    def _run_conflict_detection(self, merge_driver, base_path, ours_path, theirs_path, base_to_ours_sql, base_to_theirs_sql):
        base_to_ours_parsed = [sqlglot.parse_one(sql) for sql in base_to_ours_sql]
        base_to_theirs_parsed = [sqlglot.parse_one(sql) for sql in base_to_theirs_sql]

        base_con = sqlite3.connect(base_path)
        ours_con = sqlite3.connect(ours_path)
        theirs_con = sqlite3.connect(theirs_path)
        base_con.row_factory = sqlite3.Row
        ours_con.row_factory = sqlite3.Row
        theirs_con.row_factory = sqlite3.Row
        base_cursor = base_con.cursor()
        ours_cursor = ours_con.cursor()
        theirs_cursor = theirs_con.cursor()

        invalid_tables = merge_driver.check_valid_tables(
            base_to_ours_parsed,
            base_to_theirs_parsed,
            base_cursor,
            ours_cursor,
            theirs_cursor,
        )
        diffs = merge_driver.check_conflict_and_return_final_diff(
            base_to_ours_parsed,
            base_to_theirs_parsed,
            invalid_tables,
            base_cursor,
            ours_cursor,
            theirs_cursor,
        )

        base_con.close()
        ours_con.close()
        theirs_con.close()
        return diffs
    
    def test_no_conflicts_identical_changes(self, temp_db_three_way, merge_driver):
        """Both sides make identical changes -> no conflict."""
        base_path, ours_path, theirs_path = temp_db_three_way
        # Both insert the same row
        base_to_ours_sql = [
            "INSERT INTO users (id, name, email) VALUES (3, 'David', 'david@example.com')"
        ]
        base_to_theirs_sql = [
            "INSERT INTO users (id, name, email) VALUES (3, 'David', 'david@example.com')"
        ]
        
        diffs = self._run_conflict_detection(
            merge_driver, base_path, ours_path, theirs_path, base_to_ours_sql, base_to_theirs_sql
        )
        
        assert len(diffs.conflict_pairs) == 0
        assert len(diffs.matched_ours_indices) == 1
    
    def test_conflict_insert_different_values(self, temp_db_three_way, merge_driver):
        """Both sides insert same row with different values -> conflict."""
        base_path, ours_path, theirs_path = temp_db_three_way
        base_to_ours_sql = [
            "INSERT INTO users (id, name, email) VALUES (3, 'David', 'david@example.com')"
        ]
        base_to_theirs_sql = [
            "INSERT INTO users (id, name, email) VALUES (3, 'Diana', 'diana@example.com')"
        ]
        
        diffs = self._run_conflict_detection(
            merge_driver, base_path, ours_path, theirs_path, base_to_ours_sql, base_to_theirs_sql
        )
        print(diffs.conflict_pairs)
        assert len(diffs.conflict_pairs) == 1
    
    def test_no_conflict_extra_statements(self, temp_db_three_way, merge_driver):
        """One side makes changes, other side doesn't -> no conflict."""
        base_path, ours_path, theirs_path = temp_db_three_way
        base_to_ours_sql = [
            "INSERT INTO users (id, name, email) VALUES (3, 'David', 'david@example.com')"
        ]
        base_to_theirs_sql = []
        
        diffs = self._run_conflict_detection(
            merge_driver, base_path, ours_path, theirs_path, base_to_ours_sql, base_to_theirs_sql
        )
        
        assert len(diffs.conflict_pairs) == 0
        assert len(diffs.extra_ours_indices) == 1
    
    def test_update_delete_conflict(self, temp_db_three_way, merge_driver):
        """One side updates a row, other side deletes it -> conflict."""
        base_path, ours_path, theirs_path = temp_db_three_way
        base_to_ours_sql = [
            "UPDATE users SET email = 'new_alice@example.com' WHERE id = 1"
        ]
        base_to_theirs_sql = [
            "DELETE FROM users WHERE id = 1"
        ]
        
        diffs = self._run_conflict_detection(
            merge_driver, base_path, ours_path, theirs_path, base_to_ours_sql, base_to_theirs_sql
        )
        
        assert len(diffs.conflict_pairs) == 1
    
    def test_identical_updates_no_conflict(self, temp_db_three_way, merge_driver):
        """Both sides make identical updates -> no conflict."""
        base_path, ours_path, theirs_path = temp_db_three_way
        base_to_ours_sql = [
            "UPDATE users SET email = 'new_alice@example.com' WHERE id = 1"
        ]
        base_to_theirs_sql = [
            "UPDATE users SET email = 'new_alice@example.com' WHERE id = 1"
        ]
        
        diffs = self._run_conflict_detection(
            merge_driver, base_path, ours_path, theirs_path, base_to_ours_sql, base_to_theirs_sql
        )
        
        assert len(diffs.conflict_pairs) == 0
        assert len(diffs.matched_ours_indices) == 1
    
    def test_multiple_changes_only_some_conflict(self, temp_db_three_way, merge_driver):
        """Multiple changes where only some conflict."""
        base_path, ours_path, theirs_path = temp_db_three_way
        base_to_ours_sql = [
            "INSERT INTO users (id, name, email) VALUES (3, 'David', 'david@example.com')",
            "UPDATE users SET email = 'alice2@example.com' WHERE id = 1"
        ]
        base_to_theirs_sql = [
            "INSERT INTO users (id, name, email) VALUES (3, 'Diana', 'diana@example.com')",
            "UPDATE users SET email = 'alice2@example.com' WHERE id = 1"
        ]
        
        diffs = self._run_conflict_detection(
            merge_driver, base_path, ours_path, theirs_path, base_to_ours_sql, base_to_theirs_sql
        )
        
        # One conflict (insert with different values), one match (identical update)
        assert len(diffs.conflict_pairs) == 1
        assert len(diffs.matched_ours_indices) == 1
