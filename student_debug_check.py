import sqlite3

conn = sqlite3.connect('attendance.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

print('Student users in users table:')
for row in cursor.execute('SELECT id, username, role, student_id, password FROM users WHERE role = ?', ('student',)).fetchall():
    print(dict(row))

print('\nAll students:')
for row in cursor.execute('SELECT id, name, enrollment, branch_id FROM students').fetchall():
    print(dict(row))

conn.close()