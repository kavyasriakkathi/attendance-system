import os
from datetime import date, timedelta
import sqlite3

from flask import Flask, abort, redirect, render_template, request, session, url_for, flash
# from flask_socketio import SocketIO, emit, join_room

# # Initialize SocketIO
# socketio = SocketIO(app, cors_allowed_origins="*")

def get_db():
    if app.config["DATABASE"].startswith("postgresql"):
        import psycopg2
        from psycopg2.extras import RealDictCursor
        conn = psycopg2.connect(app.config["DATABASE"])
        conn.cursor_factory = RealDictCursor
        return conn
    else:
        conn = sqlite3.connect(app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        return conn

def get_placeholder():
    return "%s" if app.config["DATABASE"].startswith("postgresql") else "?"

def init_db():
    db = get_db()
    placeholder = get_placeholder()

    # ✅ Create tables
    if app.config["DATABASE"].startswith("postgresql"):
        # PostgreSQL specific
        db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            student_id INTEGER,
            FOREIGN KEY(student_id) REFERENCES students(id)
        );
        """)
        db.execute("""
        CREATE TABLE IF NOT EXISTS branches (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            location TEXT
        );
        """)
        db.execute("""
        CREATE TABLE IF NOT EXISTS subjects (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            branch_id INTEGER NOT NULL
        );
        """)
        db.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            enrollment TEXT UNIQUE NOT NULL,
            branch_id INTEGER NOT NULL,
            email TEXT
        );
        """)
        db.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id SERIAL PRIMARY KEY,
            student_id INTEGER NOT NULL,
            branch_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT
        );
        """)
        db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_student_subject_date
        ON attendance(student_id, subject_id, date);
        """)
    else:
        # SQLite
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            student_id INTEGER,
            FOREIGN KEY(student_id) REFERENCES students(id)
        );

        CREATE TABLE IF NOT EXISTS branches (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            location TEXT
        );

        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            branch_id INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            enrollment TEXT UNIQUE NOT NULL,
            branch_id INTEGER NOT NULL,
            email TEXT
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY,
            student_id INTEGER NOT NULL,
            branch_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_student_subject_date
        ON attendance(student_id, subject_id, date);
        """)

    # ✅ Admin check
    admin = db.execute(
        f"SELECT id FROM users WHERE username = {placeholder}", ("admin",)
    ).fetchone()

    if not admin:
        db.execute(
            f"INSERT INTO users (username, password, role) VALUES ({placeholder}, {placeholder}, {placeholder})",
            ("admin", generate_password_hash("admin123"), "admin"),
        )

    # ✅ Teacher check
    teacher = db.execute(
        f"SELECT id FROM users WHERE username = {placeholder}", ("teacher1",)
    ).fetchone()

    if not teacher:
        db.execute(
            f"INSERT INTO users (username, password, role) VALUES ({placeholder}, {placeholder}, {placeholder})",
            ("teacher1", generate_password_hash("1234"), "teacher"),
        )

    db.commit()
    db.close()
def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return view(**kwargs)
    return wrapped_view
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        db.close()

        if user and check_password_hash(user["password"], password):
            if user["role"] == "student":
                flash("Please use the student login page.", "error")
                return redirect(url_for("student_login"))

            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            return redirect(url_for("dashboard"))

        flash("Invalid username or password.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    db = get_db()

    branch_count = db.execute("SELECT COUNT(*) FROM branches").fetchone()[0]
    student_count = db.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    subject_count = db.execute("SELECT COUNT(*) FROM subjects").fetchone()[0]
    attendance_count = db.execute("SELECT COUNT(*) FROM attendance").fetchone()[0]

    attendance_stats = db.execute("""
        SELECT
            COUNT(CASE WHEN status='Present' THEN 1 END) as present_count,
            COUNT(*) as total_count
        FROM attendance
    """).fetchone()

    overall_percentage = 0
    if attendance_stats["total_count"] > 0:
        overall_percentage = round(
            (attendance_stats["present_count"] / attendance_stats["total_count"]) * 100, 1
        )

    subject_data = db.execute("""
        SELECT subjects.name AS name,
        ROUND(
            COUNT(CASE WHEN attendance.status='Present' THEN 1 END)*100.0 / COUNT(*),
            1
        ) AS percentage
        FROM attendance
        JOIN subjects ON attendance.subject_id = subjects.id
        GROUP BY subjects.id
    """).fetchall()

    db.close()

    return render_template(
        "dashboard.html",
        branch_count=branch_count,
        student_count=student_count,
        subject_count=subject_count,
        attendance_count=attendance_count,
        overall_percentage=overall_percentage,
        subject_data=subject_data
    )
@app.route("/branches", methods=["GET", "POST"])
@login_required
def branches():
    db = get_db()
    if request.method == "POST":
        name = request.form["name"].strip()
        location = request.form["location"].strip()
        if name:
            try:
                db.execute("INSERT INTO branches (name, location) VALUES (?, ?)", (name, location))
                db.commit()
                flash("Branch added successfully.", "success")
            except sqlite3.IntegrityError:
                flash("Branch name already exists.", "error")
        else:
            flash("Branch name is required.", "error")

    branches = db.execute("SELECT * FROM branches ORDER BY name").fetchall()
    db.close()
    return render_template("branches.html", branches=branches)


@app.route("/subjects", methods=["GET", "POST"])
@login_required
def subjects():
    db = get_db()
    branches = db.execute("SELECT * FROM branches ORDER BY name").fetchall()
    if request.method == "POST":
        name = request.form["name"].strip()
        branch_id = request.form.get("branch_id")
        if name and branch_id:
            db.execute(
                "INSERT INTO subjects (name, branch_id) VALUES (?, ?)",
                (name, branch_id),
            )
            db.commit()
            flash("Subject added successfully.", "success")
        else:
            flash("Subject name and branch are required.", "error")

    subjects = db.execute(
        "SELECT subjects.*, branches.name AS branch_name FROM subjects JOIN branches ON subjects.branch_id = branches.id ORDER BY subjects.name"
    ).fetchall()
    db.close()
    return render_template("subjects.html", subjects=subjects, branches=branches)


@app.route("/students", methods=["GET", "POST"])
@login_required
def students():
    db = get_db()
    placeholder = get_placeholder()
    branches = db.execute(f"SELECT * FROM branches ORDER BY name").fetchall()
    if request.method == "POST":
        name = request.form["name"].strip()
        enrollment = request.form["enrollment"].strip()
        email = request.form["email"].strip()
        branch_id = request.form.get("branch_id")
        if name and enrollment and branch_id:
            try:
                if app.config["DATABASE"].startswith("postgresql"):
                    cursor = db.execute(
                        f"INSERT INTO students (name, enrollment, email, branch_id) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}) RETURNING id",
                        (name, enrollment, email, branch_id),
                    )
                    student_id = cursor.fetchone()[0]
                else:
                    cursor = db.execute(
                        f"INSERT INTO students (name, enrollment, email, branch_id) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
                        (name, enrollment, email, branch_id),
                    )
                    student_id = cursor.lastrowid

                db.execute(
                    f"INSERT INTO users (username, password, role, student_id) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
                    (enrollment, generate_password_hash(enrollment[-4:]), "student", student_id),
                )
                db.commit()
                flash("Student added successfully.", "success")
            except Exception as e:
                print("DB ERROR:", e)
                flash("Enrollment or username already exists.", "error")
        else:
            flash("Student name, enrollment and branch are required.", "error")

    students = db.execute(
        f"SELECT students.*, branches.name AS branch_name FROM students JOIN branches ON students.branch_id = branches.id ORDER BY students.name"
    ).fetchall()
    db.close()
    return render_template("students.html", students=students, branches=branches)


@app.route("/student_login", methods=["GET", "POST"])
def student_login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        db.close()

        if user and user["role"] == "student" and check_password_hash(user["password"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["student_id"] = user["student_id"]
            return redirect(url_for("student_dashboard"))

        flash("Invalid student login credentials.", "error")

    return render_template("student_login.html")


@app.route("/student_dashboard")
@login_required
def student_dashboard():
    if session.get("role") != "student":
        return redirect(url_for("dashboard"))

    student_id = session.get("student_id")
    if not student_id:
        return redirect(url_for("student_login"))

    db = get_db()
    student = db.execute(
        """
        SELECT students.*, branches.name AS branch_name
        FROM students
        JOIN branches ON students.branch_id = branches.id
        WHERE students.id = ?
        """,
        (student_id,),
    ).fetchone()

    if not student:
        db.close()
        abort(404)

    attendance_records = db.execute(
        "SELECT attendance.date, attendance.status, subjects.name AS subject_name "
        "FROM attendance "
        "JOIN subjects ON attendance.subject_id = subjects.id "
        "WHERE attendance.student_id = ? "
        "ORDER BY attendance.date DESC",
        (student_id,),
    ).fetchall()

    total = len(attendance_records)
    present = len([a for a in attendance_records if a["status"] == "Present"])
    percentage = round((present / total) * 100, 1) if total > 0 else 0

    db.close()
    return render_template(
        "student_dashboard.html",
        student=student,
        attendance_records=attendance_records,
        percentage=percentage,
    )


@app.route("/student_dashboard/<int:student_id>")
@login_required
def student_dashboard_by_id(student_id):
    db = get_db()
    student = db.execute(
        """
        SELECT students.*, branches.name AS branch_name
        FROM students
        JOIN branches ON students.branch_id = branches.id
        WHERE students.id = ?
        """,
        (student_id,),
    ).fetchone()

    if not student:
        db.close()
        abort(404)

    attendance_records = db.execute(
        "SELECT attendance.date, attendance.status, subjects.name AS subject_name "
        "FROM attendance "
        "JOIN subjects ON attendance.subject_id = subjects.id "
        "WHERE attendance.student_id = ? "
        "ORDER BY attendance.date DESC",
        (student_id,),
    ).fetchall()

    total = len(attendance_records)
    present = len([a for a in attendance_records if a["status"] == "Present"])
    percentage = round((present / total) * 100, 1) if total > 0 else 0

    db.close()
    return render_template(
        "student_dashboard.html",
        student=student,
        attendance_records=attendance_records,
        percentage=percentage,
    )


@app.route("/attendance", methods=["GET", "POST"])
@login_required
def mark_attendance():
    db = get_db()
    branches = db.execute("SELECT * FROM branches ORDER BY name").fetchall()
    branch_id = request.args.get("branch_id") or ""
    subject_id = request.args.get("subject_id") or ""
    selected_date = request.args.get("date") or date.today().isoformat()
    subjects = []
    students = []
    existing_dates = []

    # Calculate previous and next dates
    current_date_obj = date.fromisoformat(selected_date)
    prev_date = (current_date_obj - timedelta(days=1)).isoformat()
    next_date = (current_date_obj + timedelta(days=1)).isoformat()

    if branch_id:
        subjects = db.execute(
            "SELECT * FROM subjects WHERE branch_id = ? ORDER BY name", (branch_id,)
        ).fetchall()
    if branch_id and subject_id:
        students = db.execute(
            "SELECT * FROM students WHERE branch_id = ? ORDER BY name", (branch_id,)
        ).fetchall()
        # Get existing attendance dates for this branch/subject
        existing_dates = db.execute(
            "SELECT date, COUNT(*) as count FROM attendance WHERE branch_id = ? AND subject_id = ? GROUP BY date ORDER BY date DESC",
            (branch_id, subject_id)
        ).fetchall()

    if request.method == "POST":
        branch_id = request.form.get("branch_id") or ""
        subject_id = request.form.get("subject_id") or ""
        selected_date = request.form.get("date") or date.today().isoformat()
        student_ids = request.form.getlist("student_id")

        if branch_id and subject_id and student_ids:
            for student_id in student_ids:
                status = request.form.get(f"status_{student_id}", "Absent")
                note = request.form.get(f"note_{student_id}", "")
                existing = db.execute(
                    "SELECT id FROM attendance WHERE student_id = ? AND subject_id = ? AND date = ?",
                    (student_id, subject_id, selected_date),
                ).fetchone()
                if existing:
                    db.execute(
                        "UPDATE attendance SET status = ?, note = ? WHERE id = ?",
                        (status, note, existing["id"]),
                    )
                else:
                    db.execute(
                        "INSERT INTO attendance (student_id, branch_id, subject_id, date, status, note) VALUES (?, ?, ?, ?, ?, ?)",
                        (student_id, branch_id, subject_id, selected_date, status, note),
                    )
            db.commit()

            # Emit real-time update (disabled for Render)
            # socketio.emit('attendance_saved', {
            #     'branch_id': branch_id,
            #     'subject_id': subject_id,
            #     'date': selected_date,
            #     'count': attendance_count,
            #     'message': f'Attendance saved for {attendance_count} students on {selected_date}'
            # })

            db.close()
            return redirect(
                url_for(
                    "attendance_success",
                    branch_id=branch_id,
                    subject_id=subject_id,
                    date=selected_date,
                )
            )
        else:
            flash("Please select a branch, subject, and mark attendance for students.", "error")

    attendance_map = {}
    if branch_id and subject_id:
        rows = db.execute(
            "SELECT student_id, status, note FROM attendance WHERE subject_id = ? AND date = ?",
            (subject_id, selected_date),
        ).fetchall()
        attendance_map = {str(row["student_id"]): row for row in rows}

    db.close()
    return render_template(
        "mark_attendance.html",
        branches=branches,
        subjects=subjects,
        students=students,
        branch_id=branch_id,
        subject_id=subject_id,
        selected_date=selected_date,
        attendance_map=attendance_map,
        existing_dates=existing_dates,
        prev_date=prev_date,
        next_date=next_date,
    )


@app.route("/attendance/success")
@login_required
def attendance_success():
    branch_id = request.args.get("branch_id") or ""
    subject_id = request.args.get("subject_id") or ""
    selected_date = request.args.get("date") or date.today().isoformat()
    db = get_db()
    branch = db.execute("SELECT name FROM branches WHERE id = ?", (branch_id,)).fetchone()
    subject = db.execute("SELECT name FROM subjects WHERE id = ?", (subject_id,)).fetchone()
    attendance_count = db.execute(
        "SELECT COUNT(*) AS count FROM attendance WHERE branch_id = ? AND subject_id = ? AND date = ?",
        (branch_id, subject_id, selected_date),
    ).fetchone()["count"]
    db.close()

    return render_template(
        "attendance_success.html",
        branch_name=branch["name"] if branch else "",
        subject_name=subject["name"] if subject else "",
        selected_date=selected_date,
        attendance_count=attendance_count,
    )


@app.route("/reports", methods=["GET", "POST"])
@login_required
def attendance_report():
    db = get_db()
    branches = db.execute("SELECT * FROM branches ORDER BY name").fetchall()
    subjects = []
    records = []
    filters = {
        "branch_id": request.args.get("branch_id") or request.form.get("branch_id"),
        "subject_id": request.args.get("subject_id") or request.form.get("subject_id"),
        "student_id": request.args.get("student_id") or request.form.get("student_id"),
        "from_date": request.args.get("from_date") or request.form.get("from_date"),
        "to_date": request.args.get("to_date") or request.form.get("to_date"),
    }

    if filters["branch_id"]:
        subjects = db.execute(
            "SELECT * FROM subjects WHERE branch_id = ? ORDER BY name", (filters["branch_id"],)
        ).fetchall()

    query = "SELECT attendance.*, students.name AS student_name, students.enrollment, branches.name AS branch_name, subjects.name AS subject_name FROM attendance JOIN students ON attendance.student_id = students.id JOIN branches ON attendance.branch_id = branches.id JOIN subjects ON attendance.subject_id = subjects.id"
    clauses = []
    params = []

    if filters["branch_id"]:
        clauses.append("attendance.branch_id = ?")
        params.append(filters["branch_id"])
    if filters["subject_id"]:
        clauses.append("attendance.subject_id = ?")
        params.append(filters["subject_id"])
    if filters["student_id"]:
        clauses.append("attendance.student_id = ?")
        params.append(filters["student_id"])
    if filters["from_date"]:
        clauses.append("attendance.date >= ?")
        params.append(filters["from_date"])
    if filters["to_date"]:
        clauses.append("attendance.date <= ?")
        params.append(filters["to_date"])

    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    query += " ORDER BY attendance.date DESC, students.name"
    records = db.execute(query, params).fetchall()
    students = []
    if filters["branch_id"]:
        students = db.execute(
            "SELECT * FROM students WHERE branch_id = ? ORDER BY name", (filters["branch_id"],)
        ).fetchall()

    # Calculate attendance percentages
    stats = {}
    if records:
        # Student-wise attendance
        student_stats = {}
        subject_stats = {}
        total_records = len(records)

        for record in records:
            student_id = record["student_id"]
            subject_id = record["subject_id"]
            status = record["status"]

            # Student stats
            if student_id not in student_stats:
                student_stats[student_id] = {"total": 0, "present": 0, "name": record["student_name"], "enrollment": record["enrollment"]}
            student_stats[student_id]["total"] += 1
            if status == "Present":
                student_stats[student_id]["present"] += 1

            # Subject stats
            if subject_id not in subject_stats:
                subject_stats[subject_id] = {"total": 0, "present": 0, "name": record["subject_name"]}
            subject_stats[subject_id]["total"] += 1
            if status == "Present":
                subject_stats[subject_id]["present"] += 1

        # Calculate percentages
        for student_id, data in student_stats.items():
            data["percentage"] = round((data["present"] / data["total"]) * 100, 1) if data["total"] > 0 else 0

        for subject_id, data in subject_stats.items():
            data["percentage"] = round((data["present"] / data["total"]) * 100, 1) if data["total"] > 0 else 0

        stats = {
            "student_stats": list(student_stats.values()),
            "subject_stats": list(subject_stats.values()),
            "total_records": total_records,
            "overall_present": sum(s["present"] for s in student_stats.values()),
            "overall_total": sum(s["total"] for s in student_stats.values())
        }
        if stats["overall_total"] > 0:
            stats["overall_percentage"] = round((stats["overall_present"] / stats["overall_total"]) * 100, 1)
        else:
            stats["overall_percentage"] = 0

    db.close()
    return render_template(
        "attendance_report.html",
        branches=branches,
        subjects=subjects,
        students=students,
        records=records,
        filters=filters,
        stats=stats,
    )


# # SocketIO Event Handlers for Real-time Updates (disabled for Render)
# @socketio.on('connect')
# def handle_connect():
#     print('Client connected')
#     emit('status', {'message': 'Connected to real-time attendance system'})

# @socketio.on('disconnect')
# def handle_disconnect():
#     print('Client disconnected')

# @socketio.on('join_room')
# def handle_join_room(data):
#     """Join a room for real-time updates"""
#     room = data.get('room', 'general')
#     join_room(room)
#     emit('status', {'message': f'Joined room: {room}'})

# @socketio.on('request_stats')
# def handle_request_stats():
#     """Send current attendance statistics to client"""
#     db = get_db()
#     try:
#         # Get overall attendance stats
#         total_records = db.execute("SELECT COUNT(*) FROM attendance").fetchone()[0]
#         present_count = db.execute("SELECT COUNT(*) FROM attendance WHERE status = 'Present'").fetchone()[0]
#         overall_percentage = (present_count / total_records * 100) if total_records > 0 else 0

#         # Get today's attendance
#         today = date.today().isoformat()
#         today_count = db.execute("SELECT COUNT(*) FROM attendance WHERE date = ?", (today,)).fetchone()[0]

#         # Get recent activity (last 5 attendance records)
#         recent_activity = db.execute("""
#             SELECT attendance.date, students.name as student_name, subjects.name as subject_name,
#                    attendance.status, branches.name as branch_name
#             FROM attendance
#             JOIN students ON attendance.student_id = students.id
#             JOIN subjects ON attendance.subject_id = subjects.id
#             JOIN branches ON attendance.branch_id = branches.id
#             ORDER BY attendance.id DESC LIMIT 5
#         """).fetchall()

#         stats_data = {
#             'overall_percentage': round(overall_percentage, 1),
#             'total_records': total_records,
#             'today_count': today_count,
#             'recent_activity': [{
#                 'date': activity['date'],
#                 'student': activity['student_name'],
#                 'subject': activity['subject_name'],
#                 'status': activity['status'],
#                 'branch': activity['branch_name']
#             } for activity in recent_activity]
#         }

#         emit('stats_update', stats_data)
#     except Exception as e:
#         print(f"Error getting stats: {e}")
#         emit('error', {'message': 'Failed to load statistics'})
#     finally:
#         db.close()

# Initialize DB when app starts
with app.app_context():
    try:
        init_db()
    except Exception as e:
        print(f"Database initialization failed: {e}")

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=10000)