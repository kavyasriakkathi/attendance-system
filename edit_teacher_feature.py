from pathlib import Path

path = Path('app.py')
text = path.read_text(encoding='utf-8')

if 'def _ensure_teacher_support_schema(db):' not in text:
    marker = 'def teacher_login_required(f):\n'
    insert = '''def _ensure_teacher_support_schema(db):
    _ensure_column(db, "teachers", "password_hash", "TEXT")
    _ensure_column(db, "teachers", "phone", "TEXT")
    _ensure_column(db, "teachers", "status", "TEXT")

    if str(app.config.get("DATABASE", "")).startswith("postgres"):
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS teacher_subject_assignments (
                id SERIAL PRIMARY KEY,
                teacher_id INTEGER NOT NULL,
                subject_id INTEGER NOT NULL,
                branch_id INTEGER NOT NULL,
                section TEXT,
                semester TEXT,
                academic_year TEXT
            )
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_teacher_subject_assignments_teacher
            ON teacher_subject_assignments (teacher_id)
            """
        )
    else:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS teacher_subject_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_id INTEGER NOT NULL,
                subject_id INTEGER NOT NULL,
                branch_id INTEGER NOT NULL,
                section TEXT,
                semester TEXT,
                academic_year TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_teacher_subject_assignments_teacher
            ON teacher_subject_assignments (teacher_id);
            """
        )

    _ensure_column(db, "teacher_subject_assignments", "section", "TEXT")
    _ensure_column(db, "teacher_subject_assignments", "semester", "TEXT")
    _ensure_column(db, "teacher_subject_assignments", "academic_year", "TEXT")

    try:
        db.commit()
    except Exception:
        pass


def _get_teacher_assignments(db, teacher_id):
    placeholder = get_placeholder()
    try:
        rows = db.execute(
            f"""
            SELECT
                tsa.id,
                tsa.teacher_id,
                tsa.subject_id,
                tsa.branch_id,
                tsa.section,
                tsa.semester,
                tsa.academic_year,
                s.name AS subject_name,
                b.name AS branch_name,
                b.location AS branch_location
            FROM teacher_subject_assignments tsa
            LEFT JOIN subjects s ON s.id = tsa.subject_id
            LEFT JOIN branches b ON b.id = tsa.branch_id
            WHERE tsa.teacher_id = {placeholder}
            ORDER BY b.name, s.name, tsa.section, tsa.semester
            """,
            (teacher_id,),
        ).fetchall()
    except Exception:
        rows = []

    return [
        {
            "id": row_get(row, "id"),
            "teacher_id": row_get(row, "teacher_id"),
            "subject_id": row_get(row, "subject_id"),
            "branch_id": row_get(row, "branch_id"),
            "section": row_get(row, "section") or "",
            "semester": row_get(row, "semester") or "",
            "academic_year": row_get(row, "academic_year") or "",
            "subject_name": row_get(row, "subject_name") or "",
            "branch_name": row_get(row, "branch_name") or "",
            "branch_location": row_get(row, "branch_location") or "",
        }
        for row in rows
    ]


'''
    text = text.replace(marker, insert + marker, 1)

old = '''        assigned_branches, assigned_subjects = _resolve_teacher_assignments(db, teacher_id)
        if not assigned_branches and row_get(teacher, "branch_id") is not None:
            branch_row = db.execute(
                f"SELECT id, name, location FROM branches WHERE id = {placeholder}",
                (row_get(teacher, "branch_id"),),
            ).fetchone()
            if branch_row:
                assigned_branches = [branch_row]
        if not assigned_subjects and row_get(teacher, "subject_id") is not None:
            subject_row = db.execute(
                f"SELECT id, name, branch_id FROM subjects WHERE id = {placeholder}",
                (row_get(teacher, "subject_id"),),
            ).fetchone()
            if subject_row:
                assigned_subjects = [subject_row]
'''
new = '''        assigned_classes = _get_teacher_assignments(db, teacher_id)

        assigned_branches = []
        assigned_subjects = []

        for assignment in assigned_classes:
            branch_id = row_get(assignment, "branch_id")
            subject_id = row_get(assignment, "subject_id")
            if branch_id is not None and not any(str(row_get(item, "id")) == str(branch_id) for item in assigned_branches):
                branch_row = db.execute(
                    f"SELECT id, name, location FROM branches WHERE id = {placeholder}",
                    (branch_id,),
                ).fetchone()
                if branch_row:
                    assigned_branches.append(
                        {
                            "id": row_get(branch_row, "id"),
                            "name": row_get(branch_row, "name"),
                            "location": row_get(branch_row, "location"),
                            "section": row_get(assignment, "section") or "",
                        }
                    )
            if subject_id is not None and not any(str(row_get(item, "id")) == str(subject_id) for item in assigned_subjects):
                subject_row = db.execute(
                    f"SELECT id, name, branch_id FROM subjects WHERE id = {placeholder}",
                    (subject_id,),
                ).fetchone()
                if subject_row:
                    assigned_subjects.append(
                        {
                            "id": row_get(subject_row, "id"),
                            "name": row_get(subject_row, "name"),
                            "branch_id": row_get(subject_row, "branch_id"),
                        }
                    )

        legacy_branches, legacy_subjects = _resolve_teacher_assignments(db, teacher_id)
        if not assigned_branches and legacy_branches:
            assigned_branches = [
                {
                    "id": row_get(branch, "id"),
                    "name": row_get(branch, "name"),
                    "location": row_get(branch, "location"),
                    "section": "",
                }
                for branch in legacy_branches
            ]
        if not assigned_subjects and legacy_subjects:
            assigned_subjects = [
                {
                    "id": row_get(subject, "id"),
                    "name": row_get(subject, "name"),
                    "branch_id": row_get(subject, "branch_id"),
                }
                for subject in legacy_subjects
            ]

        if not assigned_branches and row_get(teacher, "branch_id") is not None:
            branch_row = db.execute(
                f"SELECT id, name, location FROM branches WHERE id = {placeholder}",
                (row_get(teacher, "branch_id"),),
            ).fetchone()
            if branch_row:
                assigned_branches = [{
                    "id": row_get(branch_row, "id"),
                    "name": row_get(branch_row, "name"),
                    "location": row_get(branch_row, "location"),
                    "section": "",
                }]
        if not assigned_subjects and row_get(teacher, "subject_id") is not None:
            subject_row = db.execute(
                f"SELECT id, name, branch_id FROM subjects WHERE id = {placeholder}",
                (row_get(teacher, "subject_id"),),
            ).fetchone()
            if subject_row:
                assigned_subjects = [{
                    "id": row_get(subject_row, "id"),
                    "name": row_get(subject_row, "name"),
                    "branch_id": row_get(subject_row, "branch_id"),
                }]
'''
if old in text:
    text = text.replace(old, new, 1)

old = '''        current_branch_id = session.get("teacher_branch_id") or row_get(teacher, "branch_id")
        current_subject_id = session.get("teacher_subject_id") or row_get(teacher, "subject_id")
        current_section = (session.get("teacher_section") or "").strip()
        current_branch_name = session.get("teacher_branch_name") or ""

        if current_branch_id and not current_branch_name:
'''
new = '''        current_branch_id = session.get("teacher_branch_id") or row_get(teacher, "branch_id")
        current_subject_id = session.get("teacher_subject_id") or row_get(teacher, "subject_id")
        current_section = (session.get("teacher_section") or "").strip()
        current_branch_name = session.get("teacher_branch_name") or ""

        if assigned_classes and not current_branch_id:
            first_assignment = assigned_classes[0]
            current_branch_id = row_get(first_assignment, "branch_id")
            current_subject_id = row_get(first_assignment, "subject_id")
            current_section = row_get(first_assignment, "section") or current_section
            current_branch_name = row_get(first_assignment, "branch_name") or current_branch_name

        if current_branch_id and not current_branch_name:
'''
if old in text:
    text = text.replace(old, new, 1)

old = '''        teacher_name = row_get(teacher, "name") or row_get(teacher, "username") or session.get("username") or "Teacher"

        return {
'''
new = '''        teacher_name = row_get(teacher, "name") or row_get(teacher, "username") or session.get("username") or "Teacher"

        assigned_subject_ids = [str(row_get(item, "id")) for item in assigned_subjects if row_get(item, "id") is not None]
        assigned_branch_ids = [str(row_get(item, "id")) for item in assigned_branches if row_get(item, "id") is not None]
        assigned_sections = sorted({row_get(item, "section") for item in assigned_classes if row_get(item, "section")})
        assigned_semesters = sorted({row_get(item, "semester") for item in assigned_classes if row_get(item, "semester")})

        return {
'''
if old in text:
    text = text.replace(old, new, 1)

old = '''                "assigned_subjects_count": len(assigned_subjects),
                "assigned_branches_count": len(assigned_branches),
            },
'''
new = '''                "assigned_subjects_count": len(assigned_subjects),
                "assigned_branches_count": len(assigned_branches),
                "assigned_classes": assigned_classes,
                "assigned_subject_ids": assigned_subject_ids,
                "assigned_branch_ids": assigned_branch_ids,
                "assigned_sections": assigned_sections,
                "assigned_semesters": assigned_semesters,
            },
'''
if old in text:
    text = text.replace(old, new, 1)

old = '''            "assigned_subjects_count": len(assigned_subjects),
        }
'''
new = '''            "assigned_subjects_count": len(assigned_subjects),
            "assigned_classes": assigned_classes,
            "assigned_subject_ids": assigned_subject_ids,
            "assigned_branch_ids": assigned_branch_ids,
            "assigned_sections": assigned_sections,
            "assigned_semesters": assigned_semesters,
        }
'''
if old in text:
    text = text.replace(old, new, 1)

if '@app.route("/teachers", methods=["GET", "POST"])' not in text:
    marker = '@app.route("/branches", methods=["GET", "POST"])\n@login_required\ndef branches():\n'
    insert = '''@app.route("/teachers", methods=["GET", "POST"])\n@login_required\ndef teachers_management():\n    if session.get("role") != "admin":\n        abort(403)\n\n    db = None\n    try:\n        db = get_db()\n        placeholder = get_placeholder()\n        if request.method == "POST":\n            action = (request.form.get("action") or "").strip()\n            teacher_id = (request.form.get("teacher_id") or "").strip()\n\n            if action == "add":\n                name = (request.form.get("name") or "").strip()\n                username = (request.form.get("username") or "").strip()\n                password = (request.form.get("password") or "").strip()\n                email = (request.form.get("email") or "").strip()\n                phone = (request.form.get("phone") or "").strip()\n                status = (request.form.get("status") or "active").strip() or "active"\n\n                if not name or not username or not password:\n                    flash("Name, username and password are required.", "error")\n                else:\n                    existing = db.execute(\n                        f"SELECT id FROM teachers WHERE username = {placeholder}",\n                        (username,),\n                    ).fetchone()\n                    if existing:\n                        flash("A teacher with that username already exists.", "error")\n                    else:\n                        db.execute(\n                            f"INSERT INTO teachers (name, username, password, password_hash, email, phone, status) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",\n                            (name, username, generate_password_hash(password), generate_password_hash(password), email, phone, status),\n                        )\n                        db.execute(\n                            f"INSERT INTO users (username, password, role) VALUES ({placeholder}, {placeholder}, {placeholder})",\n                            (username, generate_password_hash(password), "teacher"),\n                        )\n                        db.commit()\n                        flash("Teacher created.", "success")\n\n            elif action == "edit" and teacher_id.isdigit():\n                name = (request.form.get("name") or "").strip()\n                username = (request.form.get("username") or "").strip()\n                email = (request.form.get("email") or "").strip()\n                phone = (request.form.get("phone") or "").strip()\n                status = (request.form.get("status") or "active").strip() or "active"\n                if not name or not username:\n                    flash("Name and username are required.", "error")\n                else:\n                    db.execute(\n                        f"UPDATE teachers SET name = {placeholder}, username = {placeholder}, email = {placeholder}, phone = {placeholder}, status = {placeholder} WHERE id = {placeholder}",\n                        (name, username, email, phone, status, int(teacher_id)),\n                    )\n                    db.execute(\n                        f"UPDATE users SET username = {placeholder} WHERE id = {placeholder} AND role = {placeholder}",\n                        (username, int(teacher_id), "teacher"),\n                    )\n                    db.commit()\n                    flash("Teacher updated.", "success")\n\n            elif action == "delete" and teacher_id.isdigit():\n                db.execute(f"DELETE FROM teachers WHERE id = {placeholder}", (int(teacher_id),))\n                db.execute(f"DELETE FROM users WHERE username = {placeholder} AND role = {placeholder}", (username, "teacher"))\n                db.commit()\n                flash("Teacher deleted.", "success")\n\n            elif action == "reset_password" and teacher_id.isdigit():\n                new_password = (request.form.get("new_password") or "").strip()\n                if len(new_password) < 4:\n                    flash("Password must be at least 4 characters.", "error")\n                else:\n                    db.execute(\n                        f"UPDATE teachers SET password = {placeholder}, password_hash = {placeholder} WHERE id = {placeholder}",\n                        (generate_password_hash(new_password), generate_password_hash(new_password), int(teacher_id)),\n                    )\n                    db.execute(\n                        f"UPDATE users SET password = {placeholder} WHERE id = {placeholder} AND role = {placeholder}",\n                        (generate_password_hash(new_password), int(teacher_id), "teacher"),\n                    )\n                    db.commit()\n                    flash("Password reset successfully.", "success")\n\n        teachers = db.execute(\n            f"SELECT id, name, username, password, password_hash, email, phone, status FROM teachers ORDER BY name"\n        ).fetchall()\n        subjects = db.execute("SELECT id, name FROM subjects ORDER BY name").fetchall()\n        branches = db.execute("SELECT id, name FROM branches ORDER BY name").fetchall()\n        teacher_assignments = {}\n        rows = db.execute("SELECT teacher_id, subject_id, branch_id, section, semester, academic_year FROM teacher_subject_assignments ORDER BY teacher_id").fetchall()\n        for row in rows:\n            teacher_assignments.setdefault(row_get(row, "teacher_id"), []).append({\n                "subject_id": row_get(row, "subject_id"),\n                "branch_id": row_get(row, "branch_id"),\n                "section": row_get(row, "section") or "",\n                "semester": row_get(row, "semester") or "",\n                "academic_year": row_get(row, "academic_year") or "",\n            })\n\n        return render_template(\n            "admin_teachers.html",\n            teachers=teachers,\n            subjects=subjects,\n            branches=branches,\n            teacher_assignments=teacher_assignments,\n        )\n    finally:\n        if db is not None:\n            try:\n                db.close()\n            except Exception:\n                pass\n\n\n@app.route("/assign-teachers", methods=["GET", "POST"])\n@login_required\ndef assign_teachers():\n    if session.get("role") != "admin":\n        abort(403)\n\n    db = None\n    try:\n        db = get_db()\n        if request.method == "POST":\n            teacher_id = (request.form.get("teacher_id") or "").strip()\n            subject_id = (request.form.get("subject_id") or "").strip()\n            branch_id = (request.form.get("branch_id") or "").strip()\n            section = (request.form.get("section") or "").strip()\n            semester = (request.form.get("semester") or "").strip()\n            academic_year = (request.form.get("academic_year") or "").strip()\n            if not teacher_id or not subject_id or not branch_id:\n                flash("Teacher, subject and branch are required.", "error")\n            else:\n                db.execute(\n                    f"DELETE FROM teacher_subject_assignments WHERE teacher_id = {placeholder} AND subject_id = {placeholder} AND branch_id = {placeholder}",\n                    (int(teacher_id), int(subject_id), int(branch_id)),\n                )\n                db.execute(\n                    f"INSERT INTO teacher_subject_assignments (teacher_id, subject_id, branch_id, section, semester, academic_year) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",\n                    (int(teacher_id), int(subject_id), int(branch_id), section, semester, academic_year),\n                )\n                db.commit()\n                flash("Assignment saved.", "success")\n\n        teachers = db.execute("SELECT id, name, username FROM teachers ORDER BY name").fetchall()\n        subjects = db.execute("SELECT id, name FROM subjects ORDER BY name").fetchall()\n        branches = db.execute("SELECT id, name FROM branches ORDER BY name").fetchall()\n        assignments = db.execute(\n            "SELECT tsa.id, tsa.teacher_id, tsa.subject_id, tsa.branch_id, tsa.section, tsa.semester, tsa.academic_year, t.name AS teacher_name, s.name AS subject_name, b.name AS branch_name FROM teacher_subject_assignments tsa JOIN teachers t ON t.id = tsa.teacher_id JOIN subjects s ON s.id = tsa.subject_id JOIN branches b ON b.id = tsa.branch_id ORDER BY t.name, s.name, b.name"\n        ).fetchall()\n\n        return render_template(\n            "assign_teachers.html",\n            teachers=teachers,\n            subjects=subjects,\n            branches=branches,\n            assignments=assignments,\n        )\n    finally:\n        if db is not None:\n            try:\n                db.close()\n            except Exception:\n                pass\n\n\n'''
    text = text.replace(marker, insert + marker, 1)

path.write_text(text, encoding='utf-8')
print('updated app.py with teacher support routes')
