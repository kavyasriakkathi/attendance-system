import os
import sys
import psycopg2
import psycopg2.extras
import requests
import re
from werkzeug.security import generate_password_hash

PROD_DB_URL = "postgresql://neondb_owner:npg_tlI7cGRBogs1@ep-withered-math-apo99psx-pooler.c-7.us-east-1.aws.neon.tech/neondb?sslmode=require"
RENDER_BASE_URL = "https://attendance-system-gi39.onrender.com"

MISSING_TEACHERS = [
    # Subject: Maths
    {"name": "Radhika", "subject": "Maths", "username": "radhika"},
    {"name": "Aamani", "subject": "Maths", "username": "aamani"},
    # Subject: English
    {"name": "Vani", "subject": "English", "username": "vani"},
    {"name": "Balu", "subject": "English", "username": "balu"},
    {"name": "Sandeep", "subject": "English", "username": "sandeep"},
    # Subject: Chemistry
    {"name": "Srinivas", "subject": "Chemistry", "username": "srinivas"},
    {"name": "Manasa", "subject": "Chemistry", "username": "manasa"},
    # Subject: Physics
    {"name": "Prashanth", "subject": "Physics", "username": "prashanth"},
    {"name": "Rajender", "subject": "Physics", "username": "rajender"},
    # Subject: BEE
    {"name": "Rushikesh", "subject": "BEE", "username": "rushikesh"},
    {"name": "Mallesham", "subject": "BEE", "username": "mallesham"},
    # Subject: PPS
    {"name": "Sateesh", "subject": "PPS", "username": "sateesh"},
    {"name": "Jyothi", "subject": "PPS", "username": "jyothi"},
    {"name": "Yamini", "subject": "PPS", "username": "yamini"},
]

def seed_production_neon():
    print("=== CONNECTING TO PRODUCTION NEON POSTGRESQL ===")
    conn = psycopg2.connect(PROD_DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # 1. Lookup or create branch CSM
    cur.execute("SELECT id, name FROM branches WHERE LOWER(TRIM(name)) = 'csm';")
    branch_row = cur.fetchone()
    if not branch_row:
        cur.execute("INSERT INTO branches (name, location) VALUES ('CSM', 'Main Block') RETURNING id;")
        branch_id = cur.fetchone()["id"]
        conn.commit()
        print(f"Created branch 'CSM' with id={branch_id}")
    else:
        branch_id = branch_row["id"]
        print(f"Found branch 'CSM' with id={branch_id}")

    # 2. Lookup or create subjects
    subject_ids = {}
    unique_subjects = sorted(list({t["subject"] for t in MISSING_TEACHERS}))
    for subj_name in unique_subjects:
        cur.execute("SELECT id FROM subjects WHERE LOWER(TRIM(name)) = %s;", (subj_name.lower(),))
        s_row = cur.fetchone()
        if not s_row:
            cur.execute("INSERT INTO subjects (name, branch_id) VALUES (%s, %s) RETURNING id;", (subj_name, branch_id))
            sid = cur.fetchone()["id"]
            conn.commit()
            print(f"Created subject '{subj_name}' with id={sid}")
        else:
            sid = s_row["id"]
            print(f"Found subject '{subj_name}' with id={sid}")
        subject_ids[subj_name] = sid

    pwd_hash = generate_password_hash("teacher123")
    created_count = 0
    skipped_count = 0

    print("\n=== INSERTING MISSING TEACHERS INTO PRODUCTION NEON ===")
    for tdata in MISSING_TEACHERS:
        name = tdata["name"]
        username = tdata["username"]
        subj_name = tdata["subject"]
        subj_id = subject_ids[subj_name]

        # Check existing in users or teachers
        cur.execute("SELECT id FROM users WHERE username = %s;", (username,))
        existing_u = cur.fetchone()
        cur.execute("SELECT id FROM teachers WHERE username = %s;", (username,))
        existing_t = cur.fetchone()

        if existing_u or existing_t:
            skipped_count += 1
            print(f"  [SKIPPED] Teacher '{name}' (username='{username}') already exists.")
            continue

        # 1. Insert into users
        cur.execute(
            "INSERT INTO users (username, password, role) VALUES (%s, %s, 'teacher') RETURNING id;",
            (username, pwd_hash)
        )
        user_id = cur.fetchone()["id"]

        # 2. Insert into teachers
        cur.execute(
            """INSERT INTO teachers (id, name, username, password, password_hash, subject_id, branch_id, subject_name, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active') RETURNING id;""",
            (user_id, name, username, pwd_hash, pwd_hash, subj_id, branch_id, subj_name)
        )
        teacher_id = cur.fetchone()["id"]

        # 3. teacher_subjects
        cur.execute("INSERT INTO teacher_subjects (teacher_id, subject_id) VALUES (%s, %s);", (teacher_id, subj_id))

        # 4. teacher_branches
        cur.execute("INSERT INTO teacher_branches (teacher_id, branch_id) VALUES (%s, %s);", (teacher_id, branch_id))

        # 5. teacher_subject_assignments
        cur.execute(
            """INSERT INTO teacher_subject_assignments (teacher_id, subject_id, branch_id, section, semester)
               VALUES (%s, %s, %s, 'A', '1');""",
            (teacher_id, subj_id, branch_id)
        )

        conn.commit()
        created_count += 1
        print(f"  [CREATED] Teacher '{name}' (username='{username}', id={teacher_id})")

    print("\n==========================================")
    print("SEEDING SUMMARY")
    print("==========================================")
    print(f"Total teachers created: {created_count}")
    print(f"Total skipped: {skipped_count}")

    print("\n==========================================")
    print("FINAL ROW COUNTS IN PRODUCTION NEON")
    print("==========================================")
    cnt_queries = [
        ("users(role='teacher')", "SELECT COUNT(*) FROM users WHERE role = 'teacher'"),
        ("teachers", "SELECT COUNT(*) FROM teachers"),
        ("teacher_subjects", "SELECT COUNT(*) FROM teacher_subjects"),
        ("teacher_branches", "SELECT COUNT(*) FROM teacher_branches"),
        ("teacher_subject_assignments", "SELECT COUNT(*) FROM teacher_subject_assignments"),
    ]
    for label, q in cnt_queries:
        cur.execute(q)
        cnt = cur.fetchone()["count"]
        print(f"  {label}: {cnt}")

    cur.close()
    conn.close()

    print("\n==========================================")
    print("VERIFYING ADMIN -> TEACHERS & LOGIN ON LIVE RENDER")
    print("==========================================")
    session = requests.Session()
    
    # 1. Admin login to view teacher list
    session.post(f"{RENDER_BASE_URL}/login", data={"username": "admin", "password": "admin123"})
    admin_t_res = session.get(f"{RENDER_BASE_URL}/admin/teachers")
    
    all_target_usernames = ["siddheshwar"] + [t["username"] for t in MISSING_TEACHERS]
    found_in_admin = [u for u in all_target_usernames if u in admin_t_res.text]
    print(f"Admin -> Teachers page displays {len(found_in_admin)}/15 real teachers.")

    # 2. Login verification for all 15 real teachers
    login_successes = []
    login_failures = []
    for uname in all_target_usernames:
        tsess = requests.Session()
        res = tsess.post(f"{RENDER_BASE_URL}/teacher_login", data={"username": uname, "password": "teacher123"}, allow_redirects=True)
        if res.status_code == 200 and ("teacher-dashboard" in res.url or "select-branch" in res.url or "Teacher" in res.text):
            login_successes.append(uname)
            print(f"  [OK] Teacher '{uname}' logged in successfully (URL: {res.url})")
        else:
            login_failures.append(uname)
            print(f"  [FAIL] Teacher '{uname}' login failed (Status: {res.status_code}, URL: {res.url})")

    print(f"\nLive Login Verification: {len(login_successes)}/15 teachers logged in successfully.")

if __name__ == "__main__":
    seed_production_neon()
