import pytest
import sqlglot


class TestConflictDetection:
    """Test the core conflict detection logic."""
    
    def test_no_conflicts_identical_changes(self, temp_db_with_data, merge_driver):
        """Both sides make identical changes -> no conflict."""
        # Both insert the same row
        base_to_ours_sql = [
            "INSERT INTO users (id, name, email) VALUES (3, 'David', 'david@example.com')"
        ]
        base_to_theirs_sql = [
            "INSERT INTO users (id, name, email) VALUES (3, 'David', 'david@example.com')"
        ]
        
        base_to_ours_parsed = [sqlglot.parse_one(sql) for sql in base_to_ours_sql]
        base_to_theirs_parsed = [sqlglot.parse_one(sql) for sql in base_to_theirs_sql]
        
        diffs = merge_driver.check_conflict_and_return_final_diff(
            base_to_ours_parsed, base_to_theirs_parsed, temp_db_with_data, "", ""
        )
        
        assert len(diffs.conflict_pairs) == 0
        assert len(diffs.matched_ours_indices) == 1
    
    def test_conflict_insert_different_values(self, temp_db_with_data, merge_driver):
        """Both sides insert same row with different values -> conflict."""
        base_to_ours_sql = [
            "INSERT INTO users (id, name, email) VALUES (3, 'David', 'david@example.com')"
        ]
        base_to_theirs_sql = [
            "INSERT INTO users (id, name, email) VALUES (3, 'Diana', 'diana@example.com')"
        ]
        
        base_to_ours_parsed = [sqlglot.parse_one(sql) for sql in base_to_ours_sql]
        base_to_theirs_parsed = [sqlglot.parse_one(sql) for sql in base_to_theirs_sql]
        
        diffs = merge_driver.check_conflict_and_return_final_diff(
            base_to_ours_parsed, base_to_theirs_parsed, temp_db_with_data, "", ""
        )
        print(diffs.conflict_pairs)
        assert len(diffs.conflict_pairs) == 1
        assert diffs.conflict_pairs[0].ours_index == 0
        assert diffs.conflict_pairs[0].theirs_index == 0
    
    def test_no_conflict_extra_statements(self, temp_db_with_data, merge_driver):
        """One side makes changes, other side doesn't -> no conflict."""
        base_to_ours_sql = [
            "INSERT INTO users (id, name, email) VALUES (3, 'David', 'david@example.com')"
        ]
        base_to_theirs_sql = []
        
        base_to_ours_parsed = [sqlglot.parse_one(sql) for sql in base_to_ours_sql]
        base_to_theirs_parsed = [sqlglot.parse_one(sql) for sql in base_to_theirs_sql]
        
        diffs = merge_driver.check_conflict_and_return_final_diff(
            base_to_ours_parsed, base_to_theirs_parsed, temp_db_with_data, "", ""
        )
        
        assert len(diffs.conflict_pairs) == 0
        assert len(diffs.extra_ours_indices) == 1
    
    def test_update_delete_conflict(self, temp_db_with_data, merge_driver):
        """One side updates a row, other side deletes it -> conflict."""
        base_to_ours_sql = [
            "UPDATE users SET email = 'new_alice@example.com' WHERE id = 1"
        ]
        base_to_theirs_sql = [
            "DELETE FROM users WHERE id = 1"
        ]
        
        base_to_ours_parsed = [sqlglot.parse_one(sql) for sql in base_to_ours_sql]
        base_to_theirs_parsed = [sqlglot.parse_one(sql) for sql in base_to_theirs_sql]
        
        diffs = merge_driver.check_conflict_and_return_final_diff(
            base_to_ours_parsed, base_to_theirs_parsed, temp_db_with_data, "", ""
        )
        
        assert len(diffs.conflict_pairs) == 1
    
    def test_identical_updates_no_conflict(self, temp_db_with_data, merge_driver):
        """Both sides make identical updates -> no conflict."""
        base_to_ours_sql = [
            "UPDATE users SET email = 'new_alice@example.com' WHERE id = 1"
        ]
        base_to_theirs_sql = [
            "UPDATE users SET email = 'new_alice@example.com' WHERE id = 1"
        ]
        
        base_to_ours_parsed = [sqlglot.parse_one(sql) for sql in base_to_ours_sql]
        base_to_theirs_parsed = [sqlglot.parse_one(sql) for sql in base_to_theirs_sql]
        
        diffs = merge_driver.check_conflict_and_return_final_diff(
            base_to_ours_parsed, base_to_theirs_parsed, temp_db_with_data, "", ""
        )
        
        assert len(diffs.conflict_pairs) == 0
        assert len(diffs.matched_ours_indices) == 1
    
    def test_multiple_changes_only_some_conflict(self, temp_db_with_data, merge_driver):
        """Multiple changes where only some conflict."""
        base_to_ours_sql = [
            "INSERT INTO users (id, name, email) VALUES (3, 'David', 'david@example.com')",
            "UPDATE users SET email = 'alice2@example.com' WHERE id = 1"
        ]
        base_to_theirs_sql = [
            "INSERT INTO users (id, name, email) VALUES (3, 'Diana', 'diana@example.com')",
            "UPDATE users SET email = 'alice2@example.com' WHERE id = 1"
        ]
        
        base_to_ours_parsed = [sqlglot.parse_one(sql) for sql in base_to_ours_sql]
        base_to_theirs_parsed = [sqlglot.parse_one(sql) for sql in base_to_theirs_sql]
        
        diffs = merge_driver.check_conflict_and_return_final_diff(
            base_to_ours_parsed, base_to_theirs_parsed, temp_db_with_data, "", ""
        )
        
        # One conflict (insert with different values), one match (identical update)
        assert len(diffs.conflict_pairs) == 1
        assert len(diffs.matched_ours_indices) == 1
