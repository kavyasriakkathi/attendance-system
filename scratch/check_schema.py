import sqlite3
import os

db_path = "attendance.db"
if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    print("Students table schema:")
    for row in cur.execute("PRAGMA table_info(students)"):
        print(row)
    print("\nBranches table schema:")
    for row in cur.execute("PRAGMA table_info(branches)"):
        print(row)
    conn.close()
else:
    print("Database not found")
