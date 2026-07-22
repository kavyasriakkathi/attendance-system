import sys
import traceback
from app import app
from flask import session

app.config['TESTING'] = True
app.config['PROPAGATE_EXCEPTIONS'] = True
app.error_handler_spec = {}

client = app.test_client()

def test_route(method, route, **kwargs):
    print(f"Testing {method} {route}")
    try:
        if method == 'GET':
            res = client.get(route, **kwargs)
        else:
            res = client.post(route, **kwargs)
        print(f"Status: {res.status_code}")
        if res.status_code >= 500:
            print(res.data.decode()[:500])
    except Exception as e:
        print("EXCEPTION CAUGHT:")
        traceback.print_exc()

test_route('GET', '/teacher_login')
test_route('POST', '/teacher_login', data={'username': 'tt', 'password': 'password123'})

with client.session_transaction() as sess:
    sess['role'] = 'teacher'
    sess['teacher_id'] = 1
    sess['user_id'] = 1

test_route('GET', '/teacher/dashboard')
test_route('GET', '/teacher-dashboard')
