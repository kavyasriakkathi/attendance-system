import sqlite3
conn = sqlite3.connect('attendance.db')
cur = conn.execute("DELETE FROM branches WHERE name LIKE 'COPILOT_BRANCH_%'")
print(f"Deleted {cur.rowcount} COPILOT_BRANCH_* rows from branches")
conn.commit()
conn.close()
