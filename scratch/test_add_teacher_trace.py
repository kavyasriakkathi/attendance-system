import os
import sys
import traceback
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

import app as app_module
from app import app, get_db, row_get

def test_add_teacher():
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = True
    client = app.test_client()
    
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["username"] = "admin"
        sess["role"] = "admin"
        
    print("\n=== SUBMITTING ADD TEACHER FORM WITH EXCEPTION PROPAGATION ===")
    try:
        response = client.post(
            "/admin/teachers",
            data={
                "action": "add",
                "name": "Siddheshwar Test",
                "username": "test_siddheshwar",
                "password": "teacher123",
                "email": "siddheshwar@example.com",
                "phone": "1234567890",
                "status": "active",
                "assign_subject_id[]": ["15"],
                "assign_branch_id[]": ["1"],
                "assign_section[]": ["A"],
                "assign_semester[]": ["1"],
            }
        )
        print("Response HTTP Status Code:", response.status_code)
        print("Response Data:", response.get_data(as_text=True)[:500])
    except Exception as e:
        print("\n*** CAPTURED FULL TRACEBACK FOR HTTP 500 ***")
        print("Exception Type:", type(e).__name__)
        print("Exception Message:", str(e))
        traceback.print_exc()

if __name__ == "__main__":
    test_add_teacher()
