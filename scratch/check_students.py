import sqlite3
import os

db_path = os.path.join(os.path.dirname(__dirname__), "attendance.db")
conn = sqlite3.connect("attendance.db")
cursor = conn.cursor()
cursor.execute("SELECT id, name, email FROM students")
for row in cursor.fetchall():
    print(row)
conn.close()
