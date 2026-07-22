import sqlite3
conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()
cursor.execute('SELECT id, username, password FROM users WHERE role="teacher"')
for row in cursor.fetchall():
    print(row)
conn.close()
