import os
import sys
import psycopg2
import psycopg2.extras
import traceback

PROD_DB_URL = os.environ.get("DATABASE_URL", "")

def inspect_neon_production():
    print("=== CONNECTING TO PRODUCTION NEON POSTGRESQL DATABASE ===")
    conn = psycopg2.connect(PROD_DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    print("\n--- 1. TABLE ROW COUNTS IN PRODUCTION NEON ---")
    tables = ["users", "teachers", "teacher_subject_assignments", "teacher_subjects", "teacher_branches", "subjects", "branches"]
    for t in tables:
        try:
            cur.execute(f"SELECT COUNT(*) as count FROM {t};")
            r = cur.fetchone()
            print(f"  * {t}: {r['count']} rows")
        except Exception as e:
            print(f"  * {t}: ERROR ({e})")
            conn.rollback()

    print("\n--- 2. USERS TABLE (role = 'teacher') IN PRODUCTION NEON ---")
    try:
        cur.execute("SELECT id, username, role FROM users WHERE role = 'teacher';")
        rows = cur.fetchall()
        print(f"Count: {len(rows)}")
        for r in rows:
            print("  ", dict(r))
    except Exception as e:
        print("ERROR:", e)
        conn.rollback()

    print("\n--- 3. TEACHERS TABLE IN PRODUCTION NEON ---")
    try:
        cur.execute("SELECT id, name, username, subject_id, branch_id, subject_name, email, phone, status FROM teachers;")
        rows = cur.fetchall()
        print(f"Count: {len(rows)}")
        for r in rows:
            print("  ", dict(r))
    except Exception as e:
        print("ERROR:", e)
        conn.rollback()

    print("\n--- 4. SCHEMA OF 'teachers' TABLE IN PRODUCTION NEON ---")
    try:
        cur.execute("""
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'teachers'
            ORDER BY ordinal_position;
        """)
        rows = cur.fetchall()
        for r in rows:
            print(f"  Column: {r['column_name']}, Type: {r['data_type']}, Nullable: {r['is_nullable']}, Default: {r['column_default']}")
    except Exception as e:
        print("ERROR:", e)
        conn.rollback()

    print("\n--- 5. SIMULATING TEACHER LOGIN & DASHBOARD QUERY ON PRODUCTION NEON ---")
    # Test teacher credentials on production database
    try:
        cur.execute("SELECT id, username, password, role FROM users WHERE role = 'teacher';")
        teachers = cur.fetchall()
        for t in teachers:
            uname = t["username"]
            uid = t["id"]
            print(f"\nTracing teacher login query for username='{uname}' (id={uid})...")
            # Query teachers table
            cur.execute("SELECT id, name FROM teachers WHERE username = %s;", (uname,))
            t_profile = cur.fetchone()
            print(f"  teachers profile query result: {t_profile}")
            if t_profile:
                tid = t_profile["id"]
                # Query assignments
                cur.execute("""
                    SELECT tsa.subject_id, tsa.branch_id, tsa.section, tsa.semester, s.name as subject_name, b.name as branch_name
                    FROM teacher_subject_assignments tsa
                    LEFT JOIN subjects s ON s.id = tsa.subject_id
                    LEFT JOIN branches b ON b.id = tsa.branch_id
                    WHERE tsa.teacher_id = %s;
                """, (tid,))
                assigns = cur.fetchall()
                print(f"  assignments query result count: {len(assigns)}")
    except Exception as e:
        print("\n*** EXCEPTION DURING TEACHER LOGIN QUERY SIMULATION ***")
        traceback.print_exc()
        conn.rollback()

    cur.close()
    conn.close()

if __name__ == "__main__":
    inspect_neon_production()
