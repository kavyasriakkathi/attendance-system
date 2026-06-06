import sqlite3

conn = sqlite3.connect('attendance.db')
conn.row_factory = sqlite3.Row

print("=== ALL DISTINCT TIMETABLE COMBINATIONS ===")
rows = conn.execute("""
    SELECT DISTINCT t.branch_id, b.name as branch_name, t.section 
    FROM timetable_entries t
    JOIN branches b ON t.branch_id = b.id
""").fetchall()
for r in rows:
    print(dict(r))

print("\n=== DISTINCT SECTIONS IN STUDENTS ===")
rows = conn.execute("SELECT DISTINCT branch_id, section FROM students").fetchall()
for r in rows:
    print(dict(r))

conn.close()
