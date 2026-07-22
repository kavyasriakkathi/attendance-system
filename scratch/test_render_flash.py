import traceback
from app import app
from flask import flash

app.config['TESTING'] = True
client = app.test_client()

with app.app_context():
    with client.session_transaction() as sess:
        sess['role'] = 'teacher'
    try:
        response = client.post('/teacher_login', data={'username': 'does_not_exist', 'password': 'abc'}, follow_redirects=True)
        print("Status code:", response.status_code)
        if response.status_code == 500:
            print("Response:", response.data.decode()[:1500])
    except Exception as e:
        print("EXCEPTION RAISED:")
        traceback.print_exc()
