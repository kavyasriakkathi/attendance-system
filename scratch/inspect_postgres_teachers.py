import os
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

# Read .env if present
env_file = root_dir / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))

import app as app_module
from app import app, get_db, row_get

def inspect_db():
    with app.app_context():
        db = get_db()
        db_config = str(app.config.get("DATABASE", ""))
        print("=== DATABASE PROVIDER & TARGET ===")
        print("DATABASE Config:", db_config)
        
        tables = ["users", "teachers", "teacher_subject_assignments", "teacher_subjects", "teacher_branches"]
        
        print("\n==========================================")
        print("1. ROW COUNTS IN SPECIFIED TABLES")
        print("==========================================")
        counts = {}
        for tbl in tables:
            try:
                res = db.execute(f"SELECT COUNT(*) AS count FROM {tbl}").fetchone()
                cnt = row_get(res, "count")
                counts[tbl] = cnt
                print(f"  * {tbl}: {cnt}")
            except Exception as e:
                print(f"  * {tbl}: ERROR -> {repr(e)}")

        print("\n==========================================")
        print("2. DISPLAY ALL TEACHER RECORDS FROM EACH TABLE")
        print("==========================================")

        print("\n--- [A] Table: users (role = 'teacher') ---")
        try:
            users = db.execute("SELECT id, username, role, student_id FROM users WHERE role = 'teacher' ORDER BY id").fetchall()
            print(f"Count: {len(users)}")
            for u in users:
                print("  ", dict(u) if hasattr(u, "keys") else u)
        except Exception as e:
            print("  Error:", repr(e))

        print("\n--- [B] Table: teachers ---")
        try:
            teachers = db.execute("SELECT * FROM teachers ORDER BY id").fetchall()
            print(f"Count: {len(teachers)}")
            for t in teachers:
                print("  ", dict(t) if hasattr(t, "keys") else t)
        except Exception as e:
            print("  Error:", repr(e))

        print("\n--- [C] Table: teacher_subject_assignments ---")
        try:
            tsa = db.execute("SELECT * FROM teacher_subject_assignments ORDER BY id").fetchall()
            print(f"Count: {len(tsa)}")
            for r in tsa:
                print("  ", dict(r) if hasattr(r, "keys") else r)
        except Exception as e:
            print("  Error:", repr(e))

        print("\n--- [D] Table: teacher_subjects ---")
        try:
            ts = db.execute("SELECT * FROM teacher_subjects ORDER BY id").fetchall()
            print(f"Count: {len(ts)}")
            for r in ts:
                print("  ", dict(r) if hasattr(r, "keys") else r)
        except Exception as e:
            print("  Error:", repr(e))

        print("\n--- [E] Table: teacher_branches ---")
        try:
            tb = db.execute("SELECT * FROM teacher_branches ORDER BY id").fetchall()
            print(f"Count: {len(tb)}")
            for r in tb:
                print("  ", dict(r) if hasattr(r, "keys") else r)
        except Exception as e:
            print("  Error:", repr(e))

        print("\n==========================================")
        print("3. JOINED / SYNTHESIZED TEACHER VIEW")
        print("   (id | username | name | role | subject | branch | section)")
        print("==========================================")
        
        # Query 1: From `teachers` table with assignments
        try:
            query = """
            SELECT 
                t.id AS teacher_id,
                t.username,
                t.name,
                'teacher' AS role,
                COALESCE(s.name, t.subject_name) AS subject,
                b.name AS branch,
                tsa.section AS section
            FROM teachers t
            LEFT JOIN users u ON u.username = t.username
            LEFT JOIN teacher_subject_assignments tsa ON tsa.teacher_id = t.id
            LEFT JOIN subjects s ON s.id = COALESCE(tsa.subject_id, t.subject_id)
            LEFT JOIN branches b ON b.id = COALESCE(tsa.branch_id, t.branch_id)
            ORDER BY t.id
            """
            rows = db.execute(query).fetchall()
            print("\nJoined View via `teachers` table:")
            for r in rows:
                print("  ", dict(r) if hasattr(r, "keys") else r)
        except Exception as e:
            print("  Error building joined view:", repr(e))

        print("\n==========================================")
        print("4. ADMIN -> TEACHERS PAGE ANALYSIS")
        print("==========================================")
        # Check Admin -> Teachers query execution logic
        teachers_list = db.execute("SELECT id, name, username, password, password_hash, email, phone, status FROM teachers ORDER BY name").fetchall()
        print(f"Admin Teachers Page query (`SELECT ... FROM teachers`) returns {len(teachers_list)} rows:")
        for t in teachers_list:
            print("  Row in `teachers` table:", dict(t) if hasattr(t, "keys") else t)

        users_teacher_role = db.execute("SELECT id, username, role FROM users WHERE role = 'teacher'").fetchall()
        print(f"\nUsers table (`SELECT ... FROM users WHERE role = 'teacher'`) returns {len(users_teacher_role)} rows:")
        for u in users_teacher_role:
            print("  Row in `users` table:", dict(u) if hasattr(u, "keys") else u)

if __name__ == "__main__":
    inspect_db()
