import sqlite3
conn = sqlite3.connect('attendance.db')
conn.row_factory = sqlite3.Row
print("Users:", [dict(r) for r in conn.execute("SELECT * FROM users WHERE role='teacher' LIMIT 5")])
print("Teachers:", [dict(r) for r in conn.execute("SELECT * FROM teachers LIMIT 5")])
