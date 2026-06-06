import sqlite3

conn = sqlite3.connect('attendance.db')
conn.row_factory = sqlite3.Row

print("=== TIMETABLE ENTRIES BY BRANCH ===")
rows = conn.execute("""
    SELECT DISTINCT t.branch_id, b.name as branch_name, t.section 
    FROM timetable_entries t
    JOIN branches b ON t.branch_id = b.id
    ORDER BY b.name, t.section
""").fetchall()
for r in rows:
    print(dict(r))

print("\n=== TIMETABLE SCHEMA ===")
for r in conn.execute("PRAGMA table_info(timetable_entries)").fetchall():
    print(dict(r))

print("\n=== BRANCH SCHEMA ===")
for r in conn.execute("PRAGMA table_info(branches)").fetchall():
    print(dict(r))

conn.close()
