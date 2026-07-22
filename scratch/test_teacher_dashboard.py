import sqlite3
from app import app

conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()
cursor.execute("INSERT OR IGNORE INTO subjects (id, name, branch_id) VALUES (1, 'Math', 1)")
cursor.execute("INSERT OR IGNORE INTO teacher_subject_assignments (teacher_id, subject_id, branch_id) VALUES (17, 1, 1)")
conn.commit()
conn.close()

client = app.test_client()
with client.session_transaction() as sess:
    sess['user_id'] = 17
    sess['teacher_id'] = 17
    sess['role'] = 'teacher'
    sess['username'] = 'tt'

print("Getting /teacher/dashboard")
response = client.get('/teacher/dashboard')
print('Status:', response.status_code)
if response.status_code == 500:
    print(response.data.decode()[:1500])
elif response.status_code == 403:
    print("403 error. Let me check get_teacher_context")
    from app import get_db, get_teacher_context
    with app.test_request_context('/teacher/dashboard'):
        sess = {} # I need to mock session for the context
