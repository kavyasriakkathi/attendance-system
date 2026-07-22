import sys
import traceback
from app import app
from flask import session

app.config['TESTING'] = True
app.config['PROPAGATE_EXCEPTIONS'] = True
app.error_handler_spec = {}

client = app.test_client()

with client.session_transaction() as sess:
    sess['role'] = 'teacher'
    sess['teacher_id'] = 1
    sess['user_id'] = 1
    sess['teacher_branch_id'] = 1 # give it a branch!

try:
    res = client.get('/teacher/dashboard')
    print(f"Status: {res.status_code}")
except Exception as e:
    print("EXCEPTION CAUGHT:")
    traceback.print_exc()

