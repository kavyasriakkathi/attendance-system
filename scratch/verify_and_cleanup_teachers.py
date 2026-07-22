import os
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

import app as app_module
from app import app, get_db, row_get

REAL_USERNAMES = [
    "siddheshwar", "radhika", "aamani",
    "vani", "balu", "sandeep",
    "srinivas", "manasa",
    "prashanth", "rajender",
    "rushikesh", "mallesham",
    "sateesh", "jyothi", "yamini"
]

TEMP_USERNAMES = [
    "teacher1", "tuser", "tt", "tt2", "teacher_trace_user", "teacher_full_test"
]

def verify_and_clean():
    client = app.test_client()
    
    print("=== STEP 1: VERIFY LOGIN FOR ALL 15 REAL TEACHERS ===")
    login_successes = []
    login_failures = []
    
    for username in REAL_USERNAMES:
        res = client.post("/teacher_login", data={"username": username, "password": "teacher123"}, follow_redirects=False)
        if res.status_code == 302 and "/teacher" in res.headers.get("Location", ""):
            # Check dashboard GET after login
            dash_res = client.get("/teacher/dashboard")
            if dash_res.status_code in (200, 302):
                login_successes.append(username)
                print(f"  [OK] Teacher '{username}' logged in successfully and accessed dashboard (HTTP {dash_res.status_code})")
            else:
                login_failures.append((username, f"Dashboard HTTP {dash_res.status_code}"))
                print(f"  [FAIL] Teacher '{username}' dashboard returned HTTP {dash_res.status_code}")
        else:
            login_failures.append((username, f"Login HTTP {res.status_code}"))
            print(f"  [FAIL] Teacher '{username}' login failed (HTTP {res.status_code})")

    print(f"\nLogin Verification Results: {len(login_successes)}/15 succeeded.")
    
    if len(login_failures) > 0:
        print("FAILURES DETECTED! Aborting temporary teacher cleanup.")
        return

    print("\n=== STEP 2: VERIFY ADMIN -> TEACHERS PAGE CONTAINS ALL 15 TEACHERS ===")
    with app.app_context():
        db = get_db()
        placeholder = app_module.get_placeholder()
        admin_teachers = db.execute("SELECT id, name, username FROM teachers ORDER BY name").fetchall()
        admin_usernames = {row_get(t, "username") for t in admin_teachers}
        print(f"Total teachers listed in Admin view query: {len(admin_teachers)}")
        missing = [u for u in REAL_USERNAMES if u not in admin_usernames]
        if missing:
            print("Missing real teachers in Admin view:", missing)
            return
        else:
            print("ALL 15 real teachers are present in Admin -> Teachers view!")

    print("\n=== STEP 3: REMOVE TEMPORARY TEST TEACHERS ===")
    with app.app_context():
        db = get_db()
        placeholder = app_module.get_placeholder()
        removed_count = 0
        for temp_uname in TEMP_USERNAMES:
            # Find teacher id
            t_row = db.execute(f"SELECT id FROM teachers WHERE username = {placeholder}", (temp_uname,)).fetchone()
            if t_row:
                tid = row_get(t_row, "id")
                db.execute(f"DELETE FROM teacher_subject_assignments WHERE teacher_id = {placeholder}", (tid,))
                db.execute(f"DELETE FROM teacher_subjects WHERE teacher_id = {placeholder}", (tid,))
                db.execute(f"DELETE FROM teacher_branches WHERE teacher_id = {placeholder}", (tid,))
                db.execute(f"DELETE FROM teachers WHERE id = {placeholder}", (tid,))
            db.execute(f"DELETE FROM users WHERE username = {placeholder} AND role = 'teacher'", (temp_uname,))
            db.commit()
            removed_count += 1
            print(f"  Removed temporary test teacher account: '{temp_uname}'")

    print("\n=== STEP 4: FINAL DATABASE RE-VERIFICATION ===")
    with app.app_context():
        db = get_db()
        final_teachers = db.execute("SELECT id, name, username FROM teachers ORDER BY name").fetchall()
        print(f"Final active teacher count in database: {len(final_teachers)}")
        for t in final_teachers:
            print(f"  Teacher ID {row_get(t, 'id')}: {row_get(t, 'name')} (username: {row_get(t, 'username')})")

if __name__ == "__main__":
    verify_and_clean()
