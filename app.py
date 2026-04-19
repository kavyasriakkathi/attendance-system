import os
from dotenv import load_dotenv

load_dotenv()

from datetime import date, timedelta
import sqlite3
import smtplib
import ssl
from email.message import EmailMessage

from flask import Flask, abort, redirect, render_template, request, session, url_for, flash
from functools import wraps
from werkzeug.security import check_password_hash, generate_password_hash
# from flask_socketio import SocketIO, emit, join_room

# # Initialize SocketIO
# socketio = SocketIO(app, cors_allowed_origins="*")

app = Flask(__name__)
app.config.from_mapping(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-key-change-in-production"),
    DATABASE=os.environ.get("DATABASE_URL", "attendance.db"),
    MAIL_SERVER=os.environ.get("MAIL_SERVER", "smtp.gmail.com"),
    MAIL_PORT=int(os.environ.get("MAIL_PORT", 587)),
    MAIL_USERNAME=os.environ.get("MAIL_USERNAME"),
    MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD"),
    MAIL_USE_TLS=os.environ.get("MAIL_USE_TLS", "True").lower() in ("true", "1", "yes"),
    MAIL_FROM=os.environ.get("MAIL_FROM", os.environ.get("MAIL_USERNAME")),
    LOW_ATTENDANCE_THRESHOLD=int(os.environ.get("LOW_ATTENDANCE_THRESHOLD", 75)),
)

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


def get_setting(db, key, default=None):
    placeholder = get_placeholder()
    row = db.execute(
        f"SELECT value FROM settings WHERE key = {placeholder}",
        (key,),
    ).fetchone()
    if row:
        try:
            return int(row["value"])
        except ValueError:
            return row["value"]
    return default


def set_setting(db, key, value):
    placeholder = get_placeholder()
    existing = db.execute(
        f"SELECT id FROM settings WHERE key = {placeholder}",
        (key,),
    ).fetchone()
    if existing:
        db.execute(
            f"UPDATE settings SET value = {placeholder} WHERE key = {placeholder}",
            (value, key),
        )
    else:
        db.execute(
            f"INSERT INTO settings (key, value) VALUES ({placeholder}, {placeholder})",
            (key, value),
        )


def send_email(subject, recipient, body):
    if not app.config["MAIL_USERNAME"] or not app.config["MAIL_PASSWORD"]:
        print("Email not sent: mail credentials not configured.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = app.config["MAIL_FROM"]
    msg["To"] = recipient
    msg.set_content(body)

    try:
        if app.config["MAIL_USE_TLS"]:
            context = ssl.create_default_context()
            with smtplib.SMTP(app.config["MAIL_SERVER"], app.config["MAIL_PORT"]) as server:
                server.starttls(context=context)
                server.login(app.config["MAIL_USERNAME"], app.config["MAIL_PASSWORD"])
                server.send_message(msg)
        else:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(app.config["MAIL_SERVER"], app.config["MAIL_PORT"], context=context) as server:
                server.login(app.config["MAIL_USERNAME"], app.config["MAIL_PASSWORD"])
                server.send_message(msg)
        print(f"Low attendance email sent to {recipient}")
    except Exception as e:
        print(f"Failed to send email to {recipient}: {e}")


def notify_low_attendance(db, student_ids):
    if not student_ids:
        return []

    placeholder = get_placeholder()
    threshold = get_setting(db, "low_attendance_threshold", app.config["LOW_ATTENDANCE_THRESHOLD"])
    query = f"""
        SELECT
            students.id AS student_id,
            students.name AS student_name,
            students.email AS email,
            ROUND(
                100.0 * SUM(CASE WHEN attendance.status = 'Present' THEN 1 ELSE 0 END) / COUNT(attendance.id),
                1
            ) AS percentage
        FROM students
        JOIN attendance ON attendance.student_id = students.id
        WHERE students.id IN ({', '.join([placeholder] * len(student_ids))})
        GROUP BY students.id
        HAVING COUNT(attendance.id) > 0
    """
    rows = db.execute(query, tuple(student_ids)).fetchall()
    emailed_students = []

    for row in rows:
        if not row["email"]:
            continue
        if row["percentage"] < threshold:
            body = (
                f"Hello {row['student_name']},\n\n"
                f"Your current attendance is {row['percentage']}%, which is below the minimum required threshold of {threshold}% for this course.\n"
                "Please attend classes regularly and check your attendance dashboard for details.\n\n"
                "If you have any questions, contact your instructor.\n\n"
                "Best regards,\n"
                "Attendance Management Team"
            )
            send_email(
                subject=f"Low Attendance Alert: {row['percentage']}%",
                recipient=row["email"],
                body=body,
            )
            emailed_students.append({
                "name": row["student_name"],
                "email": row["email"],
                "percentage": row["percentage"],
            })

    return emailed_students


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
        CREATE TABLE IF NOT EXISTS settings (
            id SERIAL PRIMARY KEY,
            key TEXT UNIQUE NOT NULL,
            value TEXT NOT NULL
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

        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            key TEXT UNIQUE NOT NULL,
            value TEXT NOT NULL
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

    # ✅ Default low attendance threshold setting
    if not db.execute(f"SELECT id FROM settings WHERE key = {placeholder}", ("low_attendance_threshold",)).fetchone():
        db.execute(
            f"INSERT INTO settings (key, value) VALUES ({placeholder}, {placeholder})",
            ("low_attendance_threshold", str(app.config["LOW_ATTENDANCE_THRESHOLD"])),
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
        placeholder = get_placeholder()
        user = db.execute(f"SELECT * FROM users WHERE username = {placeholder}", (username,)).fetchone()
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
def index():
    return render_template("index.html")


@app.route("/dashboard")
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

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if session.get("role") != "admin":
        return redirect(url_for("dashboard"))

    db = get_db()
    placeholder = get_placeholder()
    threshold = get_setting(db, "low_attendance_threshold", app.config["LOW_ATTENDANCE_THRESHOLD"])

    if request.method == "POST":
        new_threshold = request.form.get("threshold", "").strip()
        if new_threshold.isdigit():
            new_threshold = int(new_threshold)
            if 0 <= new_threshold <= 100:
                set_setting(db, "low_attendance_threshold", str(new_threshold))
                db.commit()
                app.config["LOW_ATTENDANCE_THRESHOLD"] = new_threshold
                threshold = new_threshold
                flash("Low attendance threshold updated successfully.", "success")
            else:
                flash("Threshold must be between 0 and 100.", "error")
        else:
            flash("Please enter a valid number for the threshold.", "error")

    settings = db.execute(f"SELECT key, value FROM settings ORDER BY key").fetchall()
    db.close()

    return render_template("settings.html", threshold=threshold, settings=settings)

@app.route("/branches", methods=["GET", "POST"])
@login_required
def branches():
    db = get_db()
    placeholder = get_placeholder()
    if request.method == "POST":
        name = request.form["name"].strip()
        location = request.form["location"].strip()
        if name:
            try:
                db.execute(f"INSERT INTO branches (name, location) VALUES ({placeholder}, {placeholder})", (name, location))
                db.commit()
                flash("Branch added successfully.", "success")
            except Exception:
                flash("Branch name already exists.", "error")
        else:
            flash("Branch name is required.", "error")

    branches = db.execute(f"SELECT * FROM branches ORDER BY name").fetchall()
    db.close()
    return render_template("branches.html", branches=branches)


@app.route("/subjects", methods=["GET", "POST"])
@login_required
def subjects():
    db = get_db()
    placeholder = get_placeholder()
    branches = db.execute(f"SELECT * FROM branches ORDER BY name").fetchall()
    if request.method == "POST":
        name = request.form["name"].strip()
        branch_id = request.form.get("branch_id")
        if name and branch_id:
            db.execute(
                f"INSERT INTO subjects (name, branch_id) VALUES ({placeholder}, {placeholder})",
                (name, branch_id),
            )
            db.commit()
            flash("Subject added successfully.", "success")
        else:
            flash("Subject name and branch are required.", "error")

    subjects = db.execute(
        f"SELECT subjects.*, branches.name AS branch_name FROM subjects JOIN branches ON subjects.branch_id = branches.id ORDER BY subjects.name"
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
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        db = get_db()
        placeholder = get_placeholder()
        user = db.execute(f"SELECT * FROM users WHERE username = {placeholder}", (username,)).fetchone()
        db.close()

        if user and user["role"] == "student" and check_password_hash(user["password"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["student_id"] = user.get("student_id") if isinstance(user, dict) else user["student_id"]
            return redirect(url_for("student_dashboard"))

        flash("Invalid student login credentials.", "error")

    return render_template("student_login.html")


@app.route("/teacher_login", methods=["GET", "POST"])
def teacher_login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()

        db = get_db()
        placeholder = get_placeholder()
        user = db.execute(f"SELECT * FROM users WHERE username = {placeholder}", (username,)).fetchone()
        db.close()

        if user and user["role"] == "teacher" and check_password_hash(user["password"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            return redirect(url_for("dashboard"))

        flash("Invalid teacher login credentials.", "error")

    return render_template("teacher_login.html")


@app.route("/student_dashboard")
@login_required
def student_dashboard():
    if session.get("role") != "student":
        return redirect(url_for("dashboard"))

    student_id = session.get("student_id")
    if not student_id:
        return redirect(url_for("student_login"))

    db = get_db()
    placeholder = get_placeholder()
    student = db.execute(
        f"""
        SELECT students.*, branches.name AS branch_name
        FROM students
        JOIN branches ON students.branch_id = branches.id
        WHERE students.id = {placeholder}
        """,
        (student_id,),
    ).fetchone()

    if not student:
        db.close()
        abort(404)

    attendance_records = db.execute(
        f"SELECT attendance.date, attendance.status, subjects.name AS subject_name "
        f"FROM attendance "
        f"JOIN subjects ON attendance.subject_id = subjects.id "
        f"WHERE attendance.student_id = {placeholder} "
        f"ORDER BY attendance.date DESC",
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
    placeholder = get_placeholder()
    student = db.execute(
        f"""
        SELECT students.*, branches.name AS branch_name
        FROM students
        JOIN branches ON students.branch_id = branches.id
        WHERE students.id = {placeholder}
        """,
        (student_id,),
    ).fetchone()

    if not student:
        db.close()
        abort(404)

    attendance_records = db.execute(
        f"SELECT attendance.date, attendance.status, subjects.name AS subject_name "
        f"FROM attendance "
        f"JOIN subjects ON attendance.subject_id = subjects.id "
        f"WHERE attendance.student_id = {placeholder} "
        f"ORDER BY attendance.date DESC",
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
    placeholder = get_placeholder()
    branches = db.execute(f"SELECT * FROM branches ORDER BY name").fetchall()
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
            f"SELECT * FROM subjects WHERE branch_id = {placeholder} ORDER BY name", (branch_id,)
        ).fetchall()
    if branch_id and subject_id:
        students = db.execute(
            f"SELECT * FROM students WHERE branch_id = {placeholder} ORDER BY name", (branch_id,)
        ).fetchall()
        # Get existing attendance dates for this branch/subject
        existing_dates = db.execute(
            f"SELECT date, COUNT(*) as count FROM attendance WHERE branch_id = {placeholder} AND subject_id = {placeholder} GROUP BY date ORDER BY date DESC",
            (branch_id, subject_id)
        ).fetchall()

    if request.method == "POST":
        branch_id = request.form.get("branch_id") or ""
        subject_id = request.form.get("subject_id") or ""
        selected_date = request.form.get("date") or date.today().isoformat()
        student_ids = request.form.getlist("student_id")

        if branch_id and subject_id and student_ids:
            saved_student_ids = []
            for student_id in student_ids:
                status = request.form.get(f"status_{student_id}", "Absent")
                note = request.form.get(f"note_{student_id}", "")
                existing = db.execute(
                    f"SELECT id FROM attendance WHERE student_id = {placeholder} AND subject_id = {placeholder} AND date = {placeholder}",
                    (student_id, subject_id, selected_date),
                ).fetchone()
                if existing:
                    db.execute(
                        f"UPDATE attendance SET status = {placeholder}, note = {placeholder} WHERE id = {placeholder}",
                        (status, note, existing["id"]),
                    )
                else:
                    db.execute(
                        f"INSERT INTO attendance (student_id, branch_id, subject_id, date, status, note) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
                        (student_id, branch_id, subject_id, selected_date, status, note),
                    )
                if student_id.isdigit():
                    saved_student_ids.append(int(student_id))
            db.commit()

            emailed_students = notify_low_attendance(db, saved_student_ids)
            session["attendance_email_summary"] = emailed_students

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
            f"SELECT student_id, status, note FROM attendance WHERE subject_id = {placeholder} AND date = {placeholder}",
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
    placeholder = get_placeholder()
    branch = db.execute(f"SELECT name FROM branches WHERE id = {placeholder}", (branch_id,)).fetchone()
    subject = db.execute(f"SELECT name FROM subjects WHERE id = {placeholder}", (subject_id,)).fetchone()
    attendance_count = db.execute(
        f"SELECT COUNT(*) AS count FROM attendance WHERE branch_id = {placeholder} AND subject_id = {placeholder} AND date = {placeholder}",
        (branch_id, subject_id, selected_date),
    ).fetchone()["count"]
    db.close()

    email_summary = session.pop("attendance_email_summary", [])

    return render_template(
        "attendance_success.html",
        branch_name=branch["name"] if branch else "",
        subject_name=subject["name"] if subject else "",
        selected_date=selected_date,
        attendance_count=attendance_count,
        email_summary=email_summary,
    )


@app.route("/reports", methods=["GET", "POST"])
@login_required
def attendance_report():
    db = get_db()
    placeholder = get_placeholder()
    branches = db.execute(f"SELECT * FROM branches ORDER BY name").fetchall()
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
            f"SELECT * FROM subjects WHERE branch_id = {placeholder} ORDER BY name", (filters["branch_id"],)
        ).fetchall()

    query = f"SELECT attendance.*, students.name AS student_name, students.enrollment, branches.name AS branch_name, subjects.name AS subject_name FROM attendance JOIN students ON attendance.student_id = students.id JOIN branches ON attendance.branch_id = branches.id JOIN subjects ON attendance.subject_id = subjects.id"
    clauses = []
    params = []

    if filters["branch_id"]:
        clauses.append(f"attendance.branch_id = {placeholder}")
        params.append(filters["branch_id"])
    if filters["subject_id"]:
        clauses.append(f"attendance.subject_id = {placeholder}")
        params.append(filters["subject_id"])
    if filters["student_id"]:
        clauses.append(f"attendance.student_id = {placeholder}")
        params.append(filters["student_id"])
    if filters["from_date"]:
        clauses.append(f"attendance.date >= {placeholder}")
        params.append(filters["from_date"])
    if filters["to_date"]:
        clauses.append(f"attendance.date <= {placeholder}")
        params.append(filters["to_date"])

    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    query += " ORDER BY attendance.date DESC, students.name"
    records = db.execute(query, params).fetchall()
    students = []
    if filters["branch_id"]:
        students = db.execute(
            f"SELECT * FROM students WHERE branch_id = {placeholder} ORDER BY name", (filters["branch_id"],)
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