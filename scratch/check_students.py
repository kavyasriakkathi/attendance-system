
import sqlite3
import os

basedir = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(basedir, "..", "attendance.db")

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

print("--- Students ---")
students = cursor.execute("SELECT id, name, email FROM students LIMIT 10").fetchall()
for s in students:
    print(f"ID: {s['id']}, Name: {s['name']}, Email: {s['email']}")

conn.close()
