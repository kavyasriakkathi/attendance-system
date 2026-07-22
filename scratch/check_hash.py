import sqlite3
from app import app
from werkzeug.security import check_password_hash

conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()
cursor.execute("SELECT id, username, password, role FROM users WHERE username = 'test_teacher999'")
user = cursor.fetchone()
print("User from db:", user)
if user:
    print("Role match:", user[3] == "teacher")
    print("Password match:", check_password_hash(user[2], 'password123'))
conn.close()
