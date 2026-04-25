
import sqlite3
import os

basedir = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(basedir, "..", "attendance.db") # Adjusted path since script is in scratch/

print(f"Checking DB at: {db_path}")
if not os.path.exists(db_path):
    print("DB file does not exist!")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("--- Tables ---")
tables = cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
for t in tables:
    print(t[0])

conn.close()
