import sqlite3

conn = sqlite3.connect("attendance.db")
cursor = conn.cursor()
cursor.execute("SELECT * FROM students WHERE name LIKE '%ABDULLAH%'")
print("Students matching ABDULLAH:")
for r in cursor.fetchall():
    print(r)

cursor.execute("SELECT * FROM branches WHERE id = (SELECT branch_id FROM students WHERE name LIKE '%ABDULLAH%' LIMIT 1)")
print("Branch matching ABDULLAH:")
for r in cursor.fetchall():
    print(r)

conn.close()
