import sqlite3
conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()
print(cursor.execute('SELECT sql FROM sqlite_master WHERE name="students"').fetchone())
conn.close()
