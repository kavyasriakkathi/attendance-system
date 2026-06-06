import sqlite3

conn = sqlite3.connect('attendance.db')
conn.row_factory = sqlite3.Row

print("=== BRANCHES ===")
for r in conn.execute("SELECT * FROM branches").fetchall():
    print(dict(r))

print("\n=== TIMETABLE ENTRIES ===")
for r in conn.execute("SELECT * FROM timetable_entries LIMIT 10").fetchall():
    print(dict(r))

print("\n=== STUDENTS ===")
for r in conn.execute("SELECT * FROM students LIMIT 5").fetchall():
    print(dict(r))

conn.close()
