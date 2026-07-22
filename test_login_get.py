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
        res = client.get('/teacher_login')
        print(f"GET /teacher_login Status: {res.status_code}")
    except Exception as e:
        print("EXCEPTION CAUGHT:")
        traceback.print_exc()
