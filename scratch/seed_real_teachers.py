import os
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

import app as app_module
from app import app, get_db, row_get
from werkzeug.security import generate_password_hash

# List of real teachers by subject
TEACHERS_DATA = [
    # Subject: Maths
    {"name": "Siddheshwar", "subject": "Maths", "username": "siddheshwar"},
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

def seed_teachers():
    with app.app_context():
        db = get_db()
        placeholder = app_module.get_placeholder()
        is_pg = str(app.config.get("DATABASE", "")).startswith("postgres")

        print("=== STEP 1: LOOKUP OR CREATE BRANCH 'CSM' ===")
        branch = db.execute(f"SELECT id, name FROM branches WHERE LOWER(TRIM(name)) = {placeholder}", ("csm",)).fetchone()
        if not branch:
            if is_pg:
                cur = db.execute(f"INSERT INTO branches (name, location) VALUES ({placeholder}, 'Main Block') RETURNING id", ("CSM",))
                branch_id = row_get(cur.fetchone(), "id")
            else:
                cur = db.execute(f"INSERT INTO branches (name, location) VALUES ({placeholder}, 'Main Block')", ("CSM",))
                branch_id = cur.lastrowid
            db.commit()
            print(f"Created branch 'CSM' with id={branch_id}")
        else:
            branch_id = row_get(branch, "id")
            print(f"Found existing branch 'CSM' with id={branch_id}")

        print("\n=== STEP 2: LOOKUP OR CREATE SUBJECTS ===")
        subject_ids = {}
        unique_subjects = sorted(list({t["subject"] for t in TEACHERS_DATA}))
        for subj_name in unique_subjects:
            subj = db.execute(f"SELECT id FROM subjects WHERE LOWER(TRIM(name)) = {placeholder}", (subj_name.lower(),)).fetchone()
            if not subj:
                if is_pg:
                    cur = db.execute(f"INSERT INTO subjects (name, branch_id) VALUES ({placeholder}, {placeholder}) RETURNING id", (subj_name, branch_id))
                    sid = row_get(cur.fetchone(), "id")
                else:
                    cur = db.execute(f"INSERT INTO subjects (name, branch_id) VALUES ({placeholder}, {placeholder})", (subj_name, branch_id))
                    sid = cur.lastrowid
                db.commit()
                print(f"Created subject '{subj_name}' with id={sid}")
            else:
                sid = row_get(subj, "id")
                print(f"Found subject '{subj_name}' with id={sid}")
            subject_ids[subj_name] = sid

        print("\n=== STEP 3: INSERT REAL TEACHER ACCOUNTS ===")
        pwd_hash = generate_password_hash("teacher123")
        created_count = 0
        existing_count = 0

        for tdata in TEACHERS_DATA:
            name = tdata["name"]
            username = tdata["username"]
            subj_name = tdata["subject"]
            subj_id = subject_ids[subj_name]

            # 1. Insert or get user
            user = db.execute(f"SELECT id FROM users WHERE username = {placeholder}", (username,)).fetchone()
            if not user:
                if is_pg:
                    cur = db.execute(
                        f"INSERT INTO users (username, password, role) VALUES ({placeholder}, {placeholder}, 'teacher') RETURNING id",
                        (username, pwd_hash)
                    )
                    user_id = row_get(cur.fetchone(), "id")
                else:
                    cur = db.execute(
                        f"INSERT INTO users (username, password, role) VALUES ({placeholder}, {placeholder}, 'teacher')",
                        (username, pwd_hash)
                    )
                    user_id = cur.lastrowid
                db.commit()
            else:
                user_id = row_get(user, "id")

            # 2. Insert or update teacher profile
            teacher = db.execute(f"SELECT id FROM teachers WHERE username = {placeholder}", (username,)).fetchone()
            if not teacher:
                if is_pg:
                    cur = db.execute(
                        f"INSERT INTO teachers (id, name, username, password, password_hash, subject_id, branch_id, subject_name, status) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'active') RETURNING id",
                        (user_id, name, username, pwd_hash, pwd_hash, subj_id, branch_id, subj_name)
                    )
                    teacher_id = row_get(cur.fetchone(), "id")
                else:
                    db.execute(
                        f"INSERT INTO teachers (id, name, username, password, password_hash, subject_id, branch_id, subject_name, status) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'active')",
                        (user_id, name, username, pwd_hash, pwd_hash, subj_id, branch_id, subj_name)
                    )
                    teacher_id = user_id
                db.commit()
                created_count += 1
                print(f"Created teacher '{name}' (username='{username}', id={teacher_id})")
            else:
                teacher_id = row_get(teacher, "id")
                existing_count += 1
                print(f"Teacher '{name}' already exists (id={teacher_id})")

            # 3. teacher_subjects
            ts = db.execute(f"SELECT id FROM teacher_subjects WHERE teacher_id = {placeholder} AND subject_id = {placeholder}", (teacher_id, subj_id)).fetchone()
            if not ts:
                db.execute(f"INSERT INTO teacher_subjects (teacher_id, subject_id) VALUES ({placeholder}, {placeholder})", (teacher_id, subj_id))

            # 4. teacher_branches
            tb = db.execute(f"SELECT id FROM teacher_branches WHERE teacher_id = {placeholder} AND branch_id = {placeholder}", (teacher_id, branch_id)).fetchone()
            if not tb:
                db.execute(f"INSERT INTO teacher_branches (teacher_id, branch_id) VALUES ({placeholder}, {placeholder})", (teacher_id, branch_id))

            # 5. teacher_subject_assignments (Section A, Semester 1)
            tsa = db.execute(
                f"SELECT id FROM teacher_subject_assignments WHERE teacher_id = {placeholder} AND subject_id = {placeholder} AND branch_id = {placeholder} AND section = {placeholder} AND semester = {placeholder}",
                (teacher_id, subj_id, branch_id, "A", "1")
            ).fetchone()
            if not tsa:
                db.execute(
                    f"INSERT INTO teacher_subject_assignments (teacher_id, subject_id, branch_id, section, semester) VALUES ({placeholder}, {placeholder}, {placeholder}, 'A', '1')",
                    (teacher_id, subj_id, branch_id)
                )

            db.commit()

        print(f"\nSeeding finished: {created_count} created, {existing_count} already existed.")

if __name__ == "__main__":
    seed_teachers()
