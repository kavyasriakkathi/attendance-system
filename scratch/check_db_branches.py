import sqlite3
import os

db_path = "attendance.db"
if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM branches")
    branches = cursor.fetchall()
    print("Branches in SQLite:")
    for b in branches:
        print(f"  ID: {b[0]}, Name: {b[1]}")
    
    cursor.execute("SELECT id, name, enrollment, branch_id FROM students LIMIT 20")
    students = cursor.fetchall()
    print("\nStudents in SQLite (First 20):")
    for s in students:
        print(f"  ID: {s[0]}, Name: {s[1]}, Enrollment: {s[2]}, Branch ID: {s[3]}")
    conn.close()
else:
    print("attendance.db not found locally.")
