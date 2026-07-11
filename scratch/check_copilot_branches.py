import sqlite3
conn = sqlite3.connect('attendance.db')
conn.row_factory = sqlite3.Row

bad = conn.execute("SELECT id, name FROM branches WHERE name LIKE 'COPILOT_BRANCH_%'").fetchall()
for row in bad:
    bid, bname = row['id'], row['name']
    count = conn.execute("SELECT COUNT(*) AS c FROM students WHERE branch_id = ?", (bid,)).fetchone()['c']
    print(f"id={bid}  name={bname!r}  students={count}")

conn.close()
