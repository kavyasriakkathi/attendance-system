import os
import traceback
from app import app
from flask import session

app.config['TESTING'] = True
app.config['PROPAGATE_EXCEPTIONS'] = True
app.config['SECRET_KEY'] = 'test'
app.error_handler_spec = {}

with app.test_client() as client:
    try:
        res = client.post('/teacher_login', data={'username': 'tt', 'password': 'password123'})
        print(f"POST /teacher_login Status: {res.status_code}")
        if res.status_code in [301, 302, 303, 307, 308]:
            print(f"Redirecting to {res.headers['Location']}")
            res = client.get(res.headers['Location'])
            print(f"GET {res.request.path} Status: {res.status_code}")
            if res.status_code in [301, 302, 303, 307, 308]:
                print(f"Redirecting to {res.headers['Location']}")
                res = client.get(res.headers['Location'])
                print(f"GET {res.request.path} Status: {res.status_code}")
    except Exception as e:
        print("EXCEPTION CAUGHT:")
        traceback.print_exc()
