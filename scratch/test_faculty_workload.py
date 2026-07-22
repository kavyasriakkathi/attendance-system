import sqlite3
import os
import sys

def row_get(row, col, default=None):
    if row is None:
        return default
    if hasattr(row, "keys"):
        if col in row.keys():
            val = row[col]
            return val if val is not None else default
    elif isinstance(row, dict):
        val = row.get(col)
        return val if val is not None else default
    return default

def get_placeholder():
    return "?"

def get_teacher_workload_context(db, teacher_id):
    placeholder = get_placeholder()

    teacher = db.execute(
        f"SELECT t.*, b.name AS branch_name FROM teachers t LEFT JOIN branches b ON t.branch_id = b.id WHERE t.id = {placeholder}",
        (teacher_id,),
    ).fetchone()

    if not teacher:
        return None

    teacher_name = row_get(teacher, "name") or ""
    department = row_get(teacher, "department") or row_get(teacher, "branch_name") or "General"

    entries = []
    try:
        entries = db.execute(
            f"SELECT te.*, s.name AS subject_name_ref, b.name AS branch_name_ref "
            f"FROM timetable_entries te "
            f"LEFT JOIN subjects s ON te.subject_id = s.id "
            f"LEFT JOIN branches b ON te.branch_id = b.id "
            f"WHERE te.teacher_id = {placeholder} OR UPPER(TRIM(te.faculty_name)) = {placeholder}",
            (teacher_id, teacher_name.upper().strip()),
        ).fetchall()
    except Exception:
        entries = []

    weekly_periods = len(entries)
    weekly_hours = round(weekly_periods * 1.0, 1)
    monthly_hours = round(weekly_hours * 4.2, 1)

    days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    day_periods = {day: 0 for day in days_order}
    day_schedule = {day: [] for day in days_order}

    subjects_map = {}
    sections_set = set()

    for e in entries:
        day = row_get(e, "day") or "Monday"
        if day in day_periods:
            day_periods[day] += 1

        start = row_get(e, "start_time") or ""
        end = row_get(e, "end_time") or ""
        sub_name = row_get(e, "subject_name") or row_get(e, "subject_name_ref") or "Subject"
        sec = row_get(e, "section") or "A"
        br = row_get(e, "branch_name_ref") or row_get(e, "branch") or ""
        room = row_get(e, "room") or ""

        if day in day_schedule:
            day_schedule[day].append({
                "time": f"{start} - {end}" if start and end else "Scheduled",
                "subject": sub_name,
                "section": sec,
                "branch": br,
                "room": room,
            })

        if sec:
            sections_set.add(f"{br}-{sec}" if br else sec)

        if sub_name not in subjects_map:
            subjects_map[sub_name] = {
                "name": sub_name,
                "periods": 0,
                "sections": set(),
            }
        subjects_map[sub_name]["periods"] += 1
        if sec:
            subjects_map[sub_name]["sections"].add(sec)

    subject_distribution = []
    for sname, sdata in subjects_map.items():
        pct = round((sdata["periods"] / weekly_periods * 100.0), 1) if weekly_periods > 0 else 0.0
        subject_distribution.append({
            "name": sname,
            "periods": sdata["periods"],
            "percentage": pct,
            "sections": list(sdata["sections"]),
        })

    conducted_count = 0
    try:
        c_row = db.execute(
            f"SELECT COUNT(*) AS cnt FROM attendance_sessions WHERE teacher_id = {placeholder}",
            (teacher_id,),
        ).fetchone()
        if c_row:
            conducted_count = row_get(c_row, "cnt") or 0
    except Exception:
        conducted_count = 0

    if weekly_periods > 18:
        workload_status = "Overloaded"
    elif weekly_periods < 12:
        workload_status = "Underutilized"
    else:
        workload_status = "Optimal"

    return {
        "teacher": {
            "id": teacher_id,
            "name": teacher_name,
            "email": row_get(teacher, "email") or "",
            "department": department,
            "branch_id": row_get(teacher, "branch_id"),
        },
        "weekly_periods": weekly_periods,
        "weekly_hours": weekly_hours,
        "monthly_hours": monthly_hours,
        "classes_conducted": conducted_count,
        "sections_handled_count": len(sections_set),
        "subjects_handled_count": len(subjects_map),
        "day_periods": day_periods,
        "day_schedule": day_schedule,
        "subject_distribution": subject_distribution,
        "workload_status": workload_status,
    }

def get_admin_workload_analytics(db):
    teachers = db.execute("SELECT id, name, department, branch_id FROM teachers ORDER BY name").fetchall()

    teacher_analytics = []
    department_map = {}

    total_college_periods = 0
    overloaded_count = 0
    underutilized_count = 0
    optimal_count = 0

    for t in teachers:
        tid = row_get(t, "id")
        tw = get_teacher_workload_context(db, tid)
        if not tw:
            continue

        teacher_analytics.append(tw)

        w_periods = tw["weekly_periods"]
        total_college_periods += w_periods

        if tw["workload_status"] == "Overloaded":
            overloaded_count += 1
        elif tw["workload_status"] == "Underutilized":
            underutilized_count += 1
        else:
            optimal_count += 1

        dept = tw["teacher"]["department"] or "General"
        if dept not in department_map:
            department_map[dept] = {
                "name": dept,
                "teacher_count": 0,
                "total_periods": 0,
                "overloaded": 0,
                "underutilized": 0,
            }
        department_map[dept]["teacher_count"] += 1
        department_map[dept]["total_periods"] += w_periods
        if tw["workload_status"] == "Overloaded":
            department_map[dept]["overloaded"] += 1
        elif tw["workload_status"] == "Underutilized":
            department_map[dept]["underutilized"] += 1

    department_analytics = []
    for dname, ddata in department_map.items():
        t_cnt = ddata["teacher_count"]
        avg_p = round(ddata["total_periods"] / t_cnt, 1) if t_cnt > 0 else 0.0

        if avg_p > 18:
            dept_status = "High Load"
        elif avg_p < 12:
            dept_status = "Low Load"
        else:
            dept_status = "Balanced"

        department_analytics.append({
            "department": dname,
            "teacher_count": t_cnt,
            "total_periods": ddata["total_periods"],
            "avg_periods": avg_p,
            "overloaded": ddata["overloaded"],
            "underutilized": ddata["underutilized"],
            "status": dept_status,
        })

    avg_college_periods = round(total_college_periods / len(teachers), 1) if teachers else 0.0

    return {
        "teacher_analytics": teacher_analytics,
        "department_analytics": department_analytics,
        "total_teachers": len(teachers),
        "total_college_periods": total_college_periods,
        "avg_college_periods": avg_college_periods,
        "overloaded_count": overloaded_count,
        "underutilized_count": underutilized_count,
        "optimal_count": optimal_count,
    }

def test_faculty_workload():
    print("--- Testing Faculty Workload Management Module ---")
    db_path = os.path.join("scratch", "test_workload.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Setup tables
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT,
            branch_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS teachers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT,
            department TEXT,
            branch_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS timetable_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            branch_id INTEGER,
            section TEXT,
            semester INTEGER,
            day TEXT,
            start_time TEXT,
            end_time TEXT,
            subject_id INTEGER,
            teacher_id INTEGER,
            subject_name TEXT,
            faculty_name TEXT,
            is_lab INTEGER DEFAULT 0,
            room TEXT
        );
        CREATE TABLE IF NOT EXISTS attendance_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id INTEGER,
            subject_id INTEGER,
            branch_id INTEGER,
            section TEXT,
            date TEXT,
            session_time TEXT
        );
    """)

    conn.execute("INSERT INTO branches (name) VALUES ('CSM')")
    branch_id = conn.execute("SELECT id FROM branches WHERE name = 'CSM'").fetchone()[0]

    conn.execute("INSERT INTO subjects (name, code, branch_id) VALUES ('Machine Learning', 'ML', ?)", (branch_id,))
    s1_id = conn.execute("SELECT id FROM subjects WHERE code = 'ML'").fetchone()[0]

    conn.execute("INSERT INTO subjects (name, code, branch_id) VALUES ('Deep Learning', 'DL', ?)", (branch_id,))
    s2_id = conn.execute("SELECT id FROM subjects WHERE code = 'DL'").fetchone()[0]

    # Teacher 1: Dr. Alan Turing -> 14 periods (Optimal)
    conn.execute("INSERT INTO teachers (name, email, department, branch_id) VALUES ('Dr. Alan Turing', 'turing@test.com', 'CSM', ?)", (branch_id,))
    t1_id = conn.execute("SELECT id FROM teachers WHERE name = 'Dr. Alan Turing'").fetchone()[0]

    # Teacher 2: Prof. Grace Hopper -> 20 periods (Overloaded)
    conn.execute("INSERT INTO teachers (name, email, department, branch_id) VALUES ('Prof. Grace Hopper', 'hopper@test.com', 'CSM', ?)", (branch_id,))
    t2_id = conn.execute("SELECT id FROM teachers WHERE name = 'Prof. Grace Hopper'").fetchone()[0]

    # Teacher 3: Dr. Ada Lovelace -> 6 periods (Underutilized)
    conn.execute("INSERT INTO teachers (name, email, department, branch_id) VALUES ('Dr. Ada Lovelace', 'lovelace@test.com', 'CSM', ?)", (branch_id,))
    t3_id = conn.execute("SELECT id FROM teachers WHERE name = 'Dr. Ada Lovelace'").fetchone()[0]

    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    # Teacher 1: 14 slots
    for i in range(14):
        day = days[i % len(days)]
        conn.execute(
            "INSERT INTO timetable_entries (branch_id, section, semester, day, start_time, end_time, subject_id, teacher_id, subject_name, faculty_name, room) VALUES (?, 'A', 5, ?, '09:00', '10:00', ?, ?, 'Machine Learning', 'Dr. Alan Turing', 'Room 101')",
            (branch_id, day, s1_id, t1_id)
        )

    # Teacher 2: 20 slots
    for i in range(20):
        day = days[i % len(days)]
        conn.execute(
            "INSERT INTO timetable_entries (branch_id, section, semester, day, start_time, end_time, subject_id, teacher_id, subject_name, faculty_name, room) VALUES (?, 'B', 5, ?, '10:00', '11:00', ?, ?, 'Deep Learning', 'Prof. Grace Hopper', 'Room 102')",
            (branch_id, day, s2_id, t2_id)
        )

    # Teacher 3: 6 slots
    for i in range(6):
        day = days[i % len(days)]
        conn.execute(
            "INSERT INTO timetable_entries (branch_id, section, semester, day, start_time, end_time, subject_id, teacher_id, subject_name, faculty_name, room) VALUES (?, 'C', 5, ?, '11:00', '12:00', ?, ?, 'Machine Learning', 'Dr. Ada Lovelace', 'Room 103')",
            (branch_id, day, s1_id, t3_id)
        )

    # Attendance sessions for Teacher 1
    for i in range(8):
        conn.execute(
            "INSERT INTO attendance_sessions (teacher_id, subject_id, branch_id, section, date, session_time) VALUES (?, ?, ?, 'A', '2026-07-10', '09:00-10:00')",
            (t1_id, s1_id, branch_id)
        )

    conn.commit()

    w1 = get_teacher_workload_context(conn, t1_id)
    assert w1 is not None, "Teacher 1 workload returned None"
    print(f"Teacher 1 ({w1['teacher']['name']}): {w1['weekly_periods']} periods/wk | Status: {w1['workload_status']} | Classes Conducted: {w1['classes_conducted']}")
    assert w1['weekly_periods'] == 14, f"Expected 14 periods, got {w1['weekly_periods']}"
    assert w1['workload_status'] == "Optimal", f"Expected Optimal, got {w1['workload_status']}"
    assert w1['classes_conducted'] == 8, f"Expected 8 conducted sessions, got {w1['classes_conducted']}"

    w2 = get_teacher_workload_context(conn, t2_id)
    print(f"Teacher 2 ({w2['teacher']['name']}): {w2['weekly_periods']} periods/wk | Status: {w2['workload_status']}")
    assert w2['weekly_periods'] == 20, f"Expected 20 periods, got {w2['weekly_periods']}"
    assert w2['workload_status'] == "Overloaded", f"Expected Overloaded, got {w2['workload_status']}"

    w3 = get_teacher_workload_context(conn, t3_id)
    print(f"Teacher 3 ({w3['teacher']['name']}): {w3['weekly_periods']} periods/wk | Status: {w3['workload_status']}")
    assert w3['weekly_periods'] == 6, f"Expected 6 periods, got {w3['weekly_periods']}"
    assert w3['workload_status'] == "Underutilized", f"Expected Underutilized, got {w3['workload_status']}"

    analytics = get_admin_workload_analytics(conn)
    print("\nCollege Workload Analytics Summary:")
    print(f"  Total Teachers: {analytics['total_teachers']}")
    print(f"  Total College Periods: {analytics['total_college_periods']}")
    print(f"  Avg Periods/Teacher: {analytics['avg_college_periods']}")
    print(f"  Overloaded Faculty Count: {analytics['overloaded_count']}")
    print(f"  Underutilized Faculty Count: {analytics['underutilized_count']}")
    print(f"  Optimal Faculty Count: {analytics['optimal_count']}")

    assert analytics['total_teachers'] == 3, f"Expected 3 teachers, got {analytics['total_teachers']}"
    assert analytics['total_college_periods'] == 40, f"Expected 40 periods total, got {analytics['total_college_periods']}"
    assert analytics['overloaded_count'] == 1, f"Expected 1 overloaded teacher, got {analytics['overloaded_count']}"
    assert analytics['underutilized_count'] == 1, f"Expected 1 underutilized teacher, got {analytics['underutilized_count']}"
    assert analytics['optimal_count'] == 1, f"Expected 1 optimal teacher, got {analytics['optimal_count']}"

    conn.close()
    if os.path.exists(db_path):
        os.remove(db_path)

    print("\nSUCCESS: Faculty Workload Management Module test passed!")

if __name__ == "__main__":
    test_faculty_workload()
