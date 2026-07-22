import psycopg2
from psycopg2.extras import RealDictCursor
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import verify_database_schema, get_teacher_context, _ensure_teacher_schema
from timetable import ensure_timetable_tables, auto_setup_academic_from_slots, import_slots_streaming, _is_postgres_db

def test_postgres_academic_setup():
    db_url = "postgresql://neondb_owner:npg_tlI7cGRBogs1@ep-withered-math-apo99psx-pooler.c-7.us-east-1.aws.neon.tech/neondb?sslmode=require"
    print("Connecting to live Neon PostgreSQL database...")
    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)

    print("PostgreSQL detected:", _is_postgres_db(conn))

    # Test slots
    slots = [
        {
            "branch": "CSM",
            "section": "CSM-B",
            "semester": 2,
            "day": "Tuesday",
            "start_time": "11:00",
            "end_time": "12:00",
            "subject_name": "Operating Systems",
            "sub_code": "OS",
            "faculty_name": "Dr. Linus Torvalds",
            "is_lab": 0,
            "room": "401"
        }
    ]

    print("--- 1. Testing auto_setup_academic_from_slots on PostgreSQL ---")
    summary = auto_setup_academic_from_slots(conn, slots)
    print("Auto setup summary on Postgres:", summary)

    # Verify teacher in DB
    cur = conn.cursor()
    cur.execute("SELECT id, name, username, status FROM teachers WHERE name = %s", ("Dr. Linus Torvalds",))
    t_row = cur.fetchone()
    print("Postgres Teacher Record:", t_row)
    assert t_row is not None, "Teacher Dr. Linus Torvalds should exist in PostgreSQL"

    # Verify subject in DB
    cur.execute("SELECT id, name, code FROM subjects WHERE name ILIKE %s", ("Operating Systems%",))
    s_row = cur.fetchone()
    print("Postgres Subject Record:", s_row)
    assert s_row is not None, "Subject Operating Systems should exist in PostgreSQL"

    # Verify assignment in DB
    cur.execute("SELECT * FROM teacher_subject_assignments WHERE teacher_id = %s", (t_row["id"],))
    a_rows = cur.fetchall()
    print("Postgres Teacher Assignments:", a_rows)
    assert len(a_rows) > 0, "Teacher assignment should exist in PostgreSQL"

    print("--- 2. Testing import_slots_streaming on PostgreSQL ---")
    import_res = import_slots_streaming(conn, slots)
    print("Import streaming result on Postgres:", import_res)

    conn.commit()
    conn.close()

    print("\nPOSTGRES SUCCESS: Live Neon PostgreSQL test passed!")

if __name__ == "__main__":
    test_postgres_academic_setup()
