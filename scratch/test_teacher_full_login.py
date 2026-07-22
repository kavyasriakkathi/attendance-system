import sqlite3
from werkzeug.security import generate_password_hash

conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()
cursor.execute("INSERT OR IGNORE INTO users (id, username, password, role) VALUES (999, 'test_teacher999', 'placeholder', 'teacher')")
conn.commit()
conn.close()

pw_hash = generate_password_hash('password123')

conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()
cursor.execute("UPDATE users SET password = ? WHERE id = 999", (pw_hash,))
cursor.execute("INSERT OR IGNORE INTO branches (id, name) VALUES (1, 'TestBranch')")
cursor.execute("INSERT OR IGNORE INTO teachers (id, name, username, password, branch_id) VALUES (999, 'Test Teacher', 'test_teacher999', ?, 1)", (pw_hash,))
conn.commit()
conn.close()

from app import app
app.config['TESTING'] = True
client = app.test_client()

print("Attempting to POST to /teacher_login")
response = client.post('/teacher_login', data={
    'username': 'test_teacher999',
    'password': 'password123'
}, follow_redirects=True)

print("Status code:", response.status_code)
if response.status_code == 500:
    print(response.data.decode()[:2000])
elif response.status_code == 403:
    print("403 Forbidden")
else:
    print("Success. HTML snippet:")
    print(response.data.decode()[:1000])
