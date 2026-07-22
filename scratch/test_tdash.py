import sqlite3
from app import app
from werkzeug.security import generate_password_hash
import traceback

conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()
pw_hash = generate_password_hash('password123')
cursor.execute("INSERT OR IGNORE INTO branches (id, name) VALUES (1, 'TestBranch')")
cursor.execute("INSERT OR IGNORE INTO subjects (id, name, branch_id) VALUES (1, 'Math', 1)")
cursor.execute("INSERT OR IGNORE INTO teachers (id, name, username, password, branch_id) VALUES (999, 'Test Teacher', 'tt999', ?, 1)", (pw_hash,))
cursor.execute("INSERT OR IGNORE INTO teacher_assignments (teacher_id, subject_id, branch_id) VALUES (999, 1, 1)")
conn.commit()
conn.close()

app.config['TESTING'] = True
client = app.test_client()

with client.session_transaction() as sess:
    sess['user_id'] = 999
    sess['teacher_id'] = 999
    sess['role'] = 'teacher'
    sess['username'] = 'tt999'

try:
    response = client.get('/teacher/dashboard')
    print("Status code:", response.status_code)
    if response.status_code == 500:
        print(response.data.decode()[:2000])
    elif response.status_code == 403:
        print("403 Forbidden")
    else:
        print("Success!")
except Exception as e:
    traceback.print_exc()
