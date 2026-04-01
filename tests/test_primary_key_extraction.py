import pytest
import sqlglot
from sqlglot import expressions


class TestPrimaryKeyExtraction:
    """Test primary key extraction from different SQL statements."""
    
    def test_delete_with_single_pk(self, merge_driver):
        """Test extracting primary key from DELETE statement."""
        sql = "DELETE FROM users WHERE id = 1"
        expr = sqlglot.parse_one(sql)
        key_column_to_index = {"id": 0}
        
        result = merge_driver.get_primary_key_values_from_where(key_column_to_index, expr)
        
        assert result is not None
        assert len(result) == 1
        assert isinstance(result[0], expressions.Literal)
        assert result[0].this == "1"
    
    def test_update_with_single_pk(self, merge_driver):
        """Test extracting primary key from UPDATE statement."""
        sql = "UPDATE users SET name = 'Charlie' WHERE id = 2"
        expr = sqlglot.parse_one(sql)
        key_column_to_index = {"id": 0}
        
        result = merge_driver.get_primary_key_values_from_where(key_column_to_index, expr)
        
        assert result is not None
        assert len(result) == 1
        assert result[0].this == "2"
    
    def test_delete_with_composite_pk(self, merge_driver):
        """Test extracting composite primary key from DELETE statement."""
        sql = "DELETE FROM purchases WHERE user_id = 1 AND product_id = 10"
        expr = sqlglot.parse_one(sql)
        key_column_to_index = {"user_id": 0, "product_id": 1}
        
        result = merge_driver.get_primary_key_values_from_where(key_column_to_index, expr)
        
        assert result is not None
        assert len(result) == 2
        assert result[0].this == "1"
        assert result[1].this == "10"


class TestInsertExtraction:
    """Test extracting key and non-key columns from INSERT statements."""
    
    def test_insert_single_row(self, merge_driver):
        """Test extracting values from single-row INSERT."""
        sql = "INSERT INTO users (id, name, email) VALUES (3, 'Charlie', 'charlie@example.com')"
        expr = sqlglot.parse_one(sql)
        schema = expr.this  # The column list
        row = expr.expression.expressions[0]  # First (and only) row
        key_column_to_index = {"id": 0}
        
        pk, cols = merge_driver.get_key_values_and_column_to_literal(row, schema, key_column_to_index)
        
        assert len(pk) == 1
        assert pk[0].this == "3"
        assert "name" in cols
        assert "email" in cols
        assert cols["name"].this == "Charlie"
        assert cols["email"].this == "charlie@example.com"
    
    def test_insert_composite_key(self, merge_driver):
        """Test extracting composite keys from INSERT."""
        sql = "INSERT INTO purchases (user_id, product_id, quantity) VALUES (3, 30, 7)"
        expr = sqlglot.parse_one(sql)
        schema = expr.this
        row = expr.expression.expressions[0]
        key_column_to_index = {"user_id": 0, "product_id": 1}
        
        pk, cols = merge_driver.get_key_values_and_column_to_literal(row, schema, key_column_to_index)
        
        assert len(pk) == 2
        assert pk[0].this == "3"
        assert pk[1].this == "30"
        assert cols["quantity"].this == "7"
