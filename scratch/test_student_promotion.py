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

def get_next_semester_name(current_sem):
    sem_map = {
        "Semester 1": "Semester 2",
        "Semester 2": "Semester 3",
        "Semester 3": "Semester 4",
        "Semester 4": "Semester 5",
        "Semester 5": "Semester 6",
        "Semester 6": "Semester 7",
        "Semester 7": "Semester 8",
        "Semester 8": "Graduated",
    }
    cur_str = str(current_sem or "Semester 1").strip()
    if cur_str in sem_map:
        return sem_map[cur_str]
    return "Semester 2"

def get_next_academic_year(current_ay, current_sem):
    cur_ay = str(current_ay or "2025-2026").strip()
    cur_sem = str(current_sem or "Semester 1").strip()

    if cur_sem in ["Semester 2", "Semester 4", "Semester 6", "Semester 8"]:
        try:
            parts = cur_ay.split("-")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                y1 = int(parts[0]) + 1
                y2 = int(parts[1]) + 1
                return f"{y1}-{y2}"
        except Exception:
            pass
    return cur_ay

def evaluate_student_promotion_eligibility(db, student_id):
    placeholder = get_placeholder()

    student = db.execute(
        f"SELECT s.*, b.name AS branch_name FROM students s LEFT JOIN branches b ON s.branch_id = b.id WHERE s.id = {placeholder}",
        (student_id,),
    ).fetchone()

    if not student:
        return None

    cur_sem = row_get(student, "semester") or "Semester 1"
    cur_ay = row_get(student, "academic_year") or "2025-2026"

    marks = []
    try:
        marks = db.execute(
            f"SELECT m.* FROM marks m JOIN exams e ON m.exam_id = e.id WHERE m.student_id = {placeholder} AND e.semester = {placeholder}",
            (student_id, cur_sem),
        ).fetchall()
    except Exception:
        marks = []

    total_eval = len(marks)
    passed_cnt = 0
    failed_cnt = 0
    sgpa = 0.0

    if marks:
        pct_list = []
        for m in marks:
            m_obt = row_get(m, "marks_obtained") or 0.0
            m_max = row_get(m, "max_marks") or 100.0
            pct = (m_obt / m_max * 100.0) if m_max > 0 else 0.0
            pct_list.append(pct)

            if pct >= 40.0:
                passed_cnt += 1
            else:
                failed_cnt += 1

        avg_pct = sum(pct_list) / len(pct_list) if pct_list else 0.0
        sgpa = round(avg_pct / 10.0, 2)
    else:
        sgpa = 7.5
        passed_cnt = 0
        failed_cnt = 0

    next_sem = get_next_semester_name(cur_sem)
    next_ay = get_next_academic_year(cur_ay, cur_sem)

    if failed_cnt == 0 and sgpa >= 5.0:
        status = "Promoted"
    elif 1 <= failed_cnt <= 3:
        status = "Promoted with Backlogs"
    else:
        status = "Not Eligible"

    return {
        "student": {
            "id": student_id,
            "name": row_get(student, "name"),
            "enrollment": row_get(student, "enrollment"),
            "semester": cur_sem,
            "academic_year": cur_ay,
        },
        "sgpa": sgpa,
        "passed_count": passed_cnt,
        "backlogs_count": failed_cnt,
        "next_semester": next_sem,
        "next_academic_year": next_ay,
        "status": status,
    }

def promote_single_student(db, student_id, target_semester=None, target_academic_year=None, remarks=None, promoted_by="Admin"):
    placeholder = get_placeholder()

    eval_data = evaluate_student_promotion_eligibility(db, student_id)
    if not eval_data:
        return False, "Student record not found."

    student = eval_data["student"]
    from_sem = student["semester"]
    from_ay = student["academic_year"]

    to_sem = target_semester or eval_data["next_semester"]
    to_ay = target_academic_year or eval_data["next_academic_year"]
    status = eval_data["status"]
    sgpa = eval_data["sgpa"]
    backlogs = eval_data["backlogs_count"]

    stud_status = "Graduated" if to_sem == "Graduated" else "Active"
    db.execute(
        f"UPDATE students SET semester = {placeholder}, academic_year = {placeholder}, promotion_status = {placeholder}, backlogs_count = {placeholder}, status = {placeholder} WHERE id = {placeholder}",
        (to_sem, to_ay, status, backlogs, stud_status, student_id),
    )

    db.execute(
        f"INSERT INTO student_promotion_history (student_id, from_semester, to_semester, from_academic_year, to_academic_year, sgpa, backlog_count, promotion_status, remarks, promoted_by) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
        (student_id, from_sem, to_sem, from_ay, to_ay, sgpa, backlogs, status, remarks or f"Promoted from {from_sem} to {to_sem}", promoted_by),
    )

    db.commit()
    return True, f"Successfully promoted {student['name']} from {from_sem} to {to_sem}!"

def test_student_promotion():
    print("--- Testing Student Promotion and Academic Year Management Module ---")
    db_path = os.path.join("scratch", "test_promotions.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            enrollment TEXT UNIQUE NOT NULL,
            branch_id INTEGER NOT NULL,
            section TEXT DEFAULT 'CSM-A',
            semester TEXT DEFAULT 'Semester 1',
            academic_year TEXT DEFAULT '2025-2026',
            promotion_status TEXT DEFAULT 'Eligible',
            backlogs_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Active'
        );
        CREATE TABLE IF NOT EXISTS exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_name TEXT NOT NULL,
            semester TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            exam_id INTEGER NOT NULL,
            marks_obtained REAL DEFAULT 0.0,
            max_marks REAL DEFAULT 100.0
        );
        CREATE TABLE IF NOT EXISTS student_promotion_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            from_semester TEXT NOT NULL,
            to_semester TEXT NOT NULL,
            from_academic_year TEXT,
            to_academic_year TEXT,
            sgpa REAL DEFAULT 0.0,
            backlog_count INTEGER DEFAULT 0,
            promotion_status TEXT NOT NULL,
            remarks TEXT,
            promoted_by TEXT DEFAULT 'Admin',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)

    conn.execute("INSERT INTO branches (name) VALUES ('CSM')")
    branch_id = conn.execute("SELECT id FROM branches WHERE name = 'CSM'").fetchone()[0]

    # Student 1: Alice (Sem 1, 0 backlogs -> Promoted)
    conn.execute("INSERT INTO students (name, enrollment, branch_id, semester, academic_year) VALUES ('Alice Smith', '21CSM010', ?, 'Semester 1', '2025-2026')", (branch_id,))
    s1_id = conn.execute("SELECT id FROM students WHERE enrollment = '21CSM010'").fetchone()[0]

    # Student 2: Bob (Sem 2, 2 backlogs -> Promoted with Backlogs)
    conn.execute("INSERT INTO students (name, enrollment, branch_id, semester, academic_year) VALUES ('Bob Jones', '21CSM011', ?, 'Semester 2', '2025-2026')", (branch_id,))
    s2_id = conn.execute("SELECT id FROM students WHERE enrollment = '21CSM011'").fetchone()[0]

    # Seed exams
    conn.execute("INSERT INTO exams (exam_name, semester) VALUES ('Mid-1', 'Semester 1')")
    e1_id = conn.execute("SELECT id FROM exams WHERE semester = 'Semester 1'").fetchone()[0]

    conn.execute("INSERT INTO exams (exam_name, semester) VALUES ('Semester End', 'Semester 2')")
    e2_id = conn.execute("SELECT id FROM exams WHERE semester = 'Semester 2'").fetchone()[0]

    # Alice marks: Passed (85/100)
    conn.execute("INSERT INTO marks (student_id, exam_id, marks_obtained, max_marks) VALUES (?, ?, 85.0, 100.0)", (s1_id, e1_id))

    # Bob marks: 1 passed (70/100), 2 failed (30/100, 35/100)
    conn.execute("INSERT INTO marks (student_id, exam_id, marks_obtained, max_marks) VALUES (?, ?, 70.0, 100.0)", (s2_id, e2_id))
    conn.execute("INSERT INTO marks (student_id, exam_id, marks_obtained, max_marks) VALUES (?, ?, 30.0, 100.0)", (s2_id, e2_id))
    conn.execute("INSERT INTO marks (student_id, exam_id, marks_obtained, max_marks) VALUES (?, ?, 35.0, 100.0)", (s2_id, e2_id))

    conn.commit()

    # 1. Test Alice Evaluation
    e1 = evaluate_student_promotion_eligibility(conn, s1_id)
    print(f"Alice: Current={e1['student']['semester']} | Target={e1['next_semester']} | Status={e1['status']} | Backlogs={e1['backlogs_count']}")
    assert e1['status'] == "Promoted", f"Expected Promoted, got {e1['status']}"
    assert e1['next_semester'] == "Semester 2", f"Expected Semester 2, got {e1['next_semester']}"

    # Promote Alice
    ok1, msg1 = promote_single_student(conn, s1_id)
    assert ok1 is True, msg1
    res1 = conn.execute("SELECT semester, academic_year, promotion_status FROM students WHERE id = ?", (s1_id,)).fetchone()
    assert res1['semester'] == "Semester 2", f"Expected Semester 2, got {res1['semester']}"
    print(f"Alice Promoted -> Semester: {res1['semester']}, Academic Year: {res1['academic_year']}")

    # 2. Test Bob Evaluation (Semester 2 -> Semester 3 & Year Upgrade)
    e2 = evaluate_student_promotion_eligibility(conn, s2_id)
    print(f"Bob: Current={e2['student']['semester']} ({e2['student']['academic_year']}) | Target={e2['next_semester']} ({e2['next_academic_year']}) | Status={e2['status']} | Backlogs={e2['backlogs_count']}")
    assert e2['status'] == "Promoted with Backlogs", f"Expected Promoted with Backlogs, got {e2['status']}"
    assert e2['next_semester'] == "Semester 3", f"Expected Semester 3, got {e2['next_semester']}"
    assert e2['next_academic_year'] == "2026-2027", f"Expected 2026-2027, got {e2['next_academic_year']}"

    # Promote Bob
    ok2, msg2 = promote_single_student(conn, s2_id)
    assert ok2 is True, msg2
    res2 = conn.execute("SELECT semester, academic_year, promotion_status, backlogs_count FROM students WHERE id = ?", (s2_id,)).fetchone()
    assert res2['semester'] == "Semester 3", f"Expected Semester 3, got {res2['semester']}"
    assert res2['academic_year'] == "2026-2027", f"Expected 2026-2027, got {res2['academic_year']}"
    print(f"Bob Promoted -> Semester: {res2['semester']}, Academic Year: {res2['academic_year']}, Backlogs: {res2['backlogs_count']}")

    # Check History Records
    history = conn.execute("SELECT * FROM student_promotion_history").fetchall()
    assert len(history) == 2, f"Expected 2 history records, got {len(history)}"
    print(f"\nPromotion History Records Created: {len(history)}")
    for h in history:
        print(f"  [ID #{h['id']}] Student #{h['student_id']}: {h['from_semester']} ({h['from_academic_year']}) -> {h['to_semester']} ({h['to_academic_year']}) | Status: {h['promotion_status']}")

    conn.close()
    if os.path.exists(db_path):
        os.remove(db_path)

    print("\nSUCCESS: Student Promotion and Academic Year Management Module test passed!")

if __name__ == "__main__":
    test_student_promotion()
