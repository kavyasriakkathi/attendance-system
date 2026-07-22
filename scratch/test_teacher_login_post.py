import traceback
from app import app, get_db

app.config['TESTING'] = True
client = app.test_client()

with app.app_context():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE role = 'teacher' LIMIT 1")
    teacher = cursor.fetchone()
    print("Teacher:", dict(teacher) if teacher else None)

if teacher:
    try:
        response = client.post('/teacher_login', data={
            'username': teacher['username'],
            'password': 'password123'  # Assuming standard password
        }, follow_redirects=True)
        print("Status code:", response.status_code)
        if response.status_code == 500:
            print("Response:", response.data.decode()[:1500])
    except Exception as e:
        traceback.print_exc()
