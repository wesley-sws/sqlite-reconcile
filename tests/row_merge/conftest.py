import pytest
import sqlite3
import tempfile
import os
import sys
import shutil
from pathlib import Path

# Add src directory to path so we can import the merge driver
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
import row_merge.sqlite_reconcile as sqlite_reconcile


@pytest.fixture(scope="session")
def merge_driver():
    """Return the imported merge driver module."""
    return sqlite_reconcile


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database with a simple schema."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    
    with sqlite3.connect(db_path) as con:
        con.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT
            )
        """)
        con.commit()
    
    yield db_path
    
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
def temp_db_with_data(temp_db):
    """Create a temporary database with initial data."""
    with sqlite3.connect(temp_db) as con:
        con.execute("INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@example.com')")
        con.execute("INSERT INTO users (id, name, email) VALUES (2, 'Bob', 'bob@example.com')")
        con.commit()
    
    return temp_db


@pytest.fixture
def temp_db_three_way(temp_db_with_data):
    """Create base/ours/theirs paths initialized from the same base data."""
    base_path = temp_db_with_data

    ours_fd, ours_path = tempfile.mkstemp(suffix=".db")
    theirs_fd, theirs_path = tempfile.mkstemp(suffix=".db")
    os.close(ours_fd)
    os.close(theirs_fd)

    shutil.copy2(base_path, ours_path)
    shutil.copy2(base_path, theirs_path)

    yield base_path, ours_path, theirs_path

    for path in (ours_path, theirs_path):
        if os.path.exists(path):
            os.remove(path)


@pytest.fixture
def composite_key_db():
    """Create a temporary database with composite primary key."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    
    with sqlite3.connect(db_path) as con:
        con.execute("""
            CREATE TABLE purchases (
                user_id INTEGER,
                product_id INTEGER,
                quantity INTEGER NOT NULL,
                PRIMARY KEY (user_id, product_id)
            )
        """)
        con.execute("INSERT INTO purchases (user_id, product_id, quantity) VALUES (1, 10, 5)")
        con.execute("INSERT INTO purchases (user_id, product_id, quantity) VALUES (2, 20, 3)")
        con.commit()
    
    yield db_path
    
    if os.path.exists(db_path):
        os.remove(db_path)
