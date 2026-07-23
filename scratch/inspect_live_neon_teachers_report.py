import os
import psycopg2
import psycopg2.extras

PROD_DB_URL = os.environ.get("DATABASE_URL", "")

def generate_report():
    conn = psycopg2.connect(PROD_DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("==========================================")
    print("1. ROW COUNTS IN LIVE NEON POSTGRESQL")
    print("==========================================")
    tables = [
        ("users (role='teacher')", "SELECT COUNT(*) FROM users WHERE role = 'teacher'"),
        ("teachers", "SELECT COUNT(*) FROM teachers"),
        ("teacher_subjects", "SELECT COUNT(*) FROM teacher_subjects"),
        ("teacher_branches", "SELECT COUNT(*) FROM teacher_branches"),
        ("teacher_subject_assignments", "SELECT COUNT(*) FROM teacher_subject_assignments"),
    ]
    for label, query in tables:
        cur.execute(query)
        cnt = cur.fetchone()["count"]
        print(f"  * {label}: {cnt}")

    print("\n==========================================")
    print("2. LIST EVERY TEACHER IN 'teachers' TABLE")
    print("==========================================")
    cur.execute("SELECT id, name, username, subject_id, branch_id, subject_name, email, status FROM teachers ORDER BY id;")
    teachers_rows = cur.fetchall()
    print(f"Total rows in teachers table: {len(teachers_rows)}")
    for r in teachers_rows:
        print("  ", dict(r))

    print("\n==========================================")
    print("3. LIST EVERY TEACHER IN 'users' TABLE (role='teacher')")
    print("==========================================")
    cur.execute("SELECT id, username, role FROM users WHERE role = 'teacher' ORDER BY id;")
    users_rows = cur.fetchall()
    print(f"Total rows in users table (role='teacher'): {len(users_rows)}")
    for r in users_rows:
        print("  ", dict(r))

    print("\n==========================================")
    print("4. COMPARISON & MAPPING ANALYSIS")
    print("==========================================")
    teachers_usernames = {r["username"] for r in teachers_rows}
    users_usernames = {r["username"] for r in users_rows}

    only_in_users = users_usernames - teachers_usernames
    only_in_teachers = teachers_usernames - users_usernames
    in_both = teachers_usernames & users_usernames

    print(f"Teachers existing in BOTH tables ({len(in_both)}): {sorted(list(in_both))}")
    print(f"Teachers existing ONLY in users table ({len(only_in_users)}): {sorted(list(only_in_users))}")
    print(f"Teachers existing ONLY in teachers table ({len(only_in_teachers)}): {sorted(list(only_in_teachers))}")

    # Check mappings in teacher_subject_assignments, teacher_subjects, teacher_branches
    print("\nMappings analysis for teachers in database:")
    for r in teachers_rows:
        tid = r["id"]
        uname = r["username"]
        cur.execute("SELECT COUNT(*) FROM teacher_subject_assignments WHERE teacher_id = %s;", (tid,))
        tsa_cnt = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(*) FROM teacher_subjects WHERE teacher_id = %s;", (tid,))
        ts_cnt = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(*) FROM teacher_branches WHERE teacher_id = %s;", (tid,))
        tb_cnt = cur.fetchone()["count"]
        print(f"  Teacher ID {tid} ('{uname}'): assignments={tsa_cnt}, teacher_subjects={ts_cnt}, teacher_branches={tb_cnt}")

    cur.close()
    conn.close()

if __name__ == "__main__":
    generate_report()
