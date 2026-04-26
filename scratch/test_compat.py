import sqlite3
import os

def test_row_access():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE test (id INTEGER)")
    conn.execute("INSERT INTO test VALUES (1), (2), (3)")
    
    # Test COUNT(*) with alias
    row = conn.execute("SELECT COUNT(*) AS count FROM test").fetchone()
    print(f"Count with alias: {row['count']}")
    
    # Test division by zero prevention
    row = conn.execute("SELECT COALESCE(100.0 / NULLIF(0, 0), 0) AS val").fetchone()
    print(f"Division by zero result: {row['val']}")
    
    conn.close()

if __name__ == "__main__":
    test_row_access()
