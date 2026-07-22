import sys
from app import app

app.testing = True
client = app.test_client()

# get a teacher username
import sqlite3
conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()
cursor.row_factory = sqlite3.Row
user = cursor.execute("SELECT username FROM users WHERE role='teacher' LIMIT 1").fetchone()
if not user:
    print("No teacher found.")
    sys.exit(1)
username = user['username']
conn.close()

print(f"Testing login for {username}")

response = client.post('/teacher_login', data={'username': username, 'password': 'password'}, follow_redirects=True)
print(f"Status: {response.status_code}")
if response.status_code == 500:
    print("500 Error!")
    print(response.data.decode('utf-8'))
else:
    print("Success?")
    print(response.data.decode('utf-8')[:500])
