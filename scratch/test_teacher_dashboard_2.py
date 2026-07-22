import sqlite3
from app import app
import traceback

conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()
cursor.execute("INSERT OR IGNORE INTO teachers (id, name, username, password, branch_id) VALUES (17, 'Test Teacher', 'tt', 'tt', 1)")
cursor.execute("INSERT OR IGNORE INTO users (id, username, password, role) VALUES (17, 'tt', 'tt', 'teacher')")
cursor.execute("INSERT OR IGNORE INTO subjects (id, name, branch_id) VALUES (1, 'Math', 1)")
cursor.execute("INSERT OR IGNORE INTO branches (id, name) VALUES (1, 'CS')")
cursor.execute("INSERT OR IGNORE INTO teacher_assignments (teacher_id, subject_id, branch_id) VALUES (17, 1, 1)")
conn.commit()
conn.close()

client = app.test_client()
with client.session_transaction() as sess:
    sess['user_id'] = 17
    sess['teacher_id'] = 17
    sess['role'] = 'teacher'
    sess['username'] = 'tt'
    # sess['current_branch_id'] = 1
    sess['current_subject_id'] = 1
    sess['current_branch_name'] = 'CS'

try:
    print("Getting /teacher/dashboard")
    response = client.get('/teacher/dashboard')
    print('Status:', response.status_code); print('Location:', response.headers.get('Location'))
    if response.status_code == 500:
        print('500 Error Data:', response.data.decode()[:1500])
except Exception as e:
    print("Caught exception running client.get!")
    traceback.print_exc()
