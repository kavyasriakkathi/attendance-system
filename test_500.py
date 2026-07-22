import os
from app import app
from flask import session
app.config['TESTING'] = True
app.config['SECRET_KEY'] = 'test'
with app.test_client() as client:
    with client.session_transaction() as sess:
        sess['role'] = 'teacher'
        sess['teacher_id'] = 1
        sess['user_id'] = 1
    try:
        res = client.get('/teacher/dashboard')
        print(f"Status: {res.status_code}")
        print(res.data.decode('utf-8')[:200])
    except Exception as e:
        import traceback
        traceback.print_exc()
