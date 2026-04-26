import sqlite3
import os

def test_query():
    basedir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    db_path = os.path.join(basedir, "attendance.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    try:
        print("Testing with empty string...")
        row = conn.execute("SELECT name FROM branches WHERE id = ?", ("",)).fetchone()
        print("Result:", row)
    except Exception as e:
        print("Error with empty string:", e)
        
    try:
        print("Testing with None...")
        row = conn.execute("SELECT name FROM branches WHERE id = ?", (None,)).fetchone()
        print("Result:", row)
    except Exception as e:
        print("Error with None:", e)
        
    conn.close()

if __name__ == "__main__":
    test_query()
