import traceback
from app import app, get_db
app.config['TESTING'] = True
client = app.test_client()

with app.app_context():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username FROM users WHERE role = 'teacher' LIMIT 1")
    teacher = cursor.fetchone()

if teacher:
    try:
        with client.session_transaction() as sess:
            sess['user_id'] = teacher['id']
            sess['teacher_id'] = teacher['id']
            sess['role'] = 'teacher'
            sess['username'] = teacher['username']
        response = client.get('/teacher/dashboard', follow_redirects=True)
        print("Status code:", response.status_code)
        if response.status_code == 500:
            print(response.data.decode()[:1500])
    except Exception as e:
        traceback.print_exc()
