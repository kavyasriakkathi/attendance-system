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
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from flask import jsonify, send_file
import pandas as pd
import io
# from flask_socketio import SocketIO, emit, join_room

# # Initialize SocketIO
# socketio = SocketIO(app, cors_allowed_origins="*")

app = Flask(__name__)
# Get the absolute path of the directory this file is in
basedir = os.path.abspath(os.path.dirname(__file__))

app.config.from_mapping(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-key-change-in-production"),
    # Use an absolute path for the database file in the project root
    DATABASE=os.path.join(basedir, "attendance.db") if not os.environ.get("DATABASE_URL") else os.environ.get("DATABASE_URL"),
    MAIL_SERVER=os.environ.get("MAIL_SERVER", "smtp.gmail.com").strip().strip('"'),
    MAIL_PORT=int(os.environ.get("MAIL_PORT", 587)),
    MAIL_USERNAME=os.environ.get("MAIL_USERNAME", "").strip().strip('"'),
    MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD", "").strip().strip('"'),
    MAIL_USE_TLS=os.environ.get("MAIL_USE_TLS", "True").lower() in ("true", "1", "yes"),
    MAIL_FROM=os.environ.get("MAIL_FROM", os.environ.get("MAIL_USERNAME", "")).strip().strip('"'),
    LOW_ATTENDANCE_THRESHOLD=int(os.environ.get("LOW_ATTENDANCE_THRESHOLD", 75)),
)
class PostgresConnectionWrapper:
    def __init__(self, conn):
        self.conn = conn
    def execute(self, query, params=None):
        cursor = self.conn.cursor()
        try:
            cursor.execute(query, params)
        except Exception as e:
            print(f"DATABASE EXECUTE ERROR: {e}")
            print(f"QUERY: {query}")
            print(f"PARAMS: {params}")
            raise
        return cursor
    def commit(self):
        self.conn.commit()
    def close(self):
        self.conn.close()

def get_db():
    db_url = app.config["DATABASE"]
    if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        # Fix for Render/Heroku DATABASE_URL prefix
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
            # Update the config so get_placeholder works correctly
            app.config["DATABASE"] = db_url
            
        import psycopg2
        from psycopg2.extras import RealDictCursor
        try:
            conn = psycopg2.connect(db_url)
            conn.cursor_factory = RealDictCursor
            return PostgresConnectionWrapper(conn)
        except Exception as e:
            print(f"CRITICAL: Failed to connect to PostgreSQL: {e}")
            # Fallback to SQLite if Postgres fails (only for safety, data will be ephemeral)
            sqlite_path = os.path.join(basedir, "attendance.db")
            print(f"FALLBACK: Using SQLite at {sqlite_path}")
            conn = sqlite3.connect(sqlite_path)
            conn.row_factory = sqlite3.Row
            return conn
    else:
        conn = sqlite3.connect(db_url)
        conn.row_factory = sqlite3.Row
        return conn

def get_placeholder():
    db_url = app.config["DATABASE"]
    return "%s" if db_url.startswith("postgresql") or db_url.startswith("postgres") else "?"


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


def send_email(subject, to_email, message):
    sender_email = app.config["MAIL_USERNAME"]
    app_password = app.config["MAIL_PASSWORD"]

    if not sender_email or not app_password or sender_email == "your_email@gmail.com":
        print(f"DEBUG: Email to {to_email} NOT sent: mail credentials not configured.")
        return False, "Mail credentials not configured."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = to_email
    msg.set_content(message)

    try:
        context = ssl.create_default_context()
        # Use config for server/port, but fallback to user's suggested Gmail defaults if not set
        mail_server = app.config.get("MAIL_SERVER", "smtp.gmail.com")
        mail_port = app.config.get("MAIL_PORT", 587)

        with smtplib.SMTP(mail_server, mail_port, timeout=10) as server:
            server.starttls(context=context)
            server.login(sender_email, app_password)
            server.send_message(msg)

        print("Email sent successfully")
        return True, "Email sent successfully"

    except Exception as e:
        print("Email error:", e)
        return False, str(e)


def notify_low_attendance(db, student_ids, subject_id=None):
    if not student_ids:
        return []

    placeholder = get_placeholder()
    threshold = get_setting(db, "low_attendance_threshold", app.config["LOW_ATTENDANCE_THRESHOLD"])
    
    # Filter by subject if provided, otherwise check overall attendance
    subject_clause = f"AND attendance.subject_id = {placeholder}" if subject_id else ""
    
    query = f"""
        SELECT
            students.id AS student_id,
            students.name AS student_name,
            students.email AS email,
            ROUND(
                100.0 * SUM(CASE WHEN attendance.status = 'Present' THEN 1 ELSE 0 END) / COUNT(attendance.id),
                1
            ) AS percentage,
            subjects.name AS subject_name
        FROM students
        JOIN attendance ON attendance.student_id = students.id
        JOIN subjects ON attendance.subject_id = subjects.id
        WHERE students.id IN ({', '.join([placeholder] * len(student_ids))})
        {subject_clause}
        GROUP BY students.id, subjects.id
        HAVING COUNT(attendance.id) > 0
    """
    
    params = list(student_ids)
    if subject_id:
        params.append(subject_id)
        
    rows = db.execute(query, tuple(params)).fetchall()
    emailed_students = []

    print(f"DEBUG: Checking {len(rows)} students for low attendance (threshold: {threshold}%)")

    for row in rows:
        if not row["email"] or not row["email"].strip():
            print(f"DEBUG: Student {row['student_name']} has no email address. Skipping.")
            continue
            
        if row["percentage"] < threshold:
            print(f"DEBUG: Student {row['student_name']} attendance is {row['percentage']}%. Sending email.")
            subject_name = row["subject_name"] if subject_id else "your classes"
            body = (
                f"Hello {row['student_name']},\n\n"
                f"Your current attendance for {subject_name} is {row['percentage']}%, which is below the minimum required threshold of {threshold}%.\n"
                "Please attend classes regularly and check your attendance dashboard for details.\n\n"
                "If you have any questions, contact your instructor.\n\n"
                "Best regards,\n"
                "Attendance Management Team"
            )
            success, error_msg = send_email(
                subject=f"Low Attendance Alert ({subject_name}): {row['percentage']}%",
                to_email=row["email"],
                message=body,
            )
            if success:
                emailed_students.append({
                    "name": row["student_name"],
                    "email": row["email"],
                    "percentage": row["percentage"],
                    "subject": subject_name
                })
        else:
            print(f"DEBUG: Student {row['student_name']} attendance is {row['percentage']}%. No email needed.")

    return emailed_students


def init_db():
    db = get_db()
    placeholder = get_placeholder()

    # ✅ Create tables
    if app.config["DATABASE"].startswith("postgresql") or app.config["DATABASE"].startswith("postgres"):
        # PostgreSQL specific
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

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            student_id INTEGER,
            FOREIGN KEY(student_id) REFERENCES students(id)
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
@app.errorhandler(Exception)
def handle_exception(e):
    # Pass through HTTP errors
    if hasattr(e, "code") and e.code < 500:
        return e

    # Flash the error message for debugging
    flash(f"Internal Server Error: {str(e)}", "error")
    print(f"ERROR: {str(e)}")
    import traceback
    traceback.print_exc()
    
    # Return to dashboard or login
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

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

    branch_count = db.execute("SELECT COUNT(*) AS count FROM branches").fetchone()["count"]
    student_count = db.execute("SELECT COUNT(*) AS count FROM students").fetchone()["count"]
    subject_count = db.execute("SELECT COUNT(*) AS count FROM subjects").fetchone()["count"]
    attendance_count = db.execute("SELECT COUNT(*) AS count FROM attendance").fetchone()["count"]

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

    branch_data = db.execute("""
        SELECT
            branches.name AS branch_name,
            branches.location AS location,
            COUNT(DISTINCT students.id) AS student_count,
            COUNT(DISTINCT subjects.id) AS subject_count,
            COUNT(attendance.id) AS attendance_count,
            ROUND(
                COALESCE(COUNT(CASE WHEN attendance.status='Present' THEN 1 END)*100.0 / NULLIF(COUNT(attendance.id), 0), 0),
                1
            ) AS attendance_percentage
        FROM branches
        LEFT JOIN students ON branches.id = students.branch_id
        LEFT JOIN subjects ON branches.id = subjects.branch_id
        LEFT JOIN attendance ON branches.id = attendance.branch_id
        GROUP BY branches.id, branches.name, branches.location
        ORDER BY branches.name
    """).fetchall()
    database_info = {
        "storage": "PostgreSQL" if app.config["DATABASE"].startswith("postgresql") or app.config["DATABASE"].startswith("postgres") else "SQLite",
        "path": app.config["DATABASE"] if not (app.config["DATABASE"].startswith("postgresql") or app.config["DATABASE"].startswith("postgres")) else "PostgreSQL Server (Remote)",
        "full_url": app.config["DATABASE"] if app.config["DATABASE"].startswith("sqlite") or "/" in app.config["DATABASE"] else "Managed Service",
        "is_ephemeral": bool(os.environ.get("RENDER") or os.environ.get("RENDER_INTERNAL_HOSTNAME")) and not (app.config["DATABASE"].startswith("postgresql") or app.config["DATABASE"].startswith("postgres"))
    }
    mail_info = {
        "configured": bool(app.config["MAIL_USERNAME"] and app.config["MAIL_PASSWORD"]),
        "server": app.config["MAIL_SERVER"],
        "port": app.config["MAIL_PORT"],
        "username": app.config["MAIL_USERNAME"],
        "tls": app.config["MAIL_USE_TLS"],
        "render_env": bool(os.environ.get("RENDER") or os.environ.get("RENDER_INTERNAL_HOSTNAME")),
    }

    # Fetch attendance trend for charts (last 7 days)
    chart_data = []
    placeholder = get_placeholder()
    for i in range(6, -1, -1):
        target_date = (date.today() - timedelta(days=i)).isoformat()
        day_stats = db.execute(f"""
            SELECT 
                COUNT(CASE WHEN status='Present' THEN 1 END) as present_count,
                COUNT(*) as total_count
            FROM attendance 
            WHERE date = {placeholder}
        """, (target_date,)).fetchone()
        
        pct = round((day_stats["present_count"] / day_stats["total_count"] * 100), 1) if day_stats["total_count"] > 0 else 0
        chart_data.append({"date": target_date, "percentage": pct})

    db.close()

    return render_template(
        "dashboard.html",
        branch_count=branch_count,
        student_count=student_count,
        subject_count=subject_count,
        attendance_count=attendance_count,
        overall_percentage=overall_percentage,
        subject_data=subject_data,
        branch_data=branch_data,
        database_info=database_info,
        mail_info=mail_info,
        persistence_warning=database_info["is_ephemeral"],
        chart_data=chart_data
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

    mail_info = {
        "configured": bool(app.config["MAIL_USERNAME"] and app.config["MAIL_PASSWORD"]),
        "server": app.config["MAIL_SERVER"],
        "port": app.config["MAIL_PORT"],
        "username": app.config["MAIL_USERNAME"],
        "tls": app.config["MAIL_USE_TLS"],
        "render_env": bool(os.environ.get("RENDER") or os.environ.get("RENDER_INTERNAL_HOSTNAME")),
    }

    return render_template(
        "settings.html",
        threshold=threshold,
        settings=settings,
        mail_info=mail_info,
    )

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
                    student_id = cursor.fetchone()["id"]
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
        next_url = request.form.get("next") or request.args.get("next")

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
            
            if next_url and next_url.startswith("/"):
                return redirect(next_url)
            return redirect(url_for("student_dashboard"))

        flash("Invalid student login credentials.", "error")

    return render_template("student_login.html")


@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        db = get_db()
        placeholder = get_placeholder()
        
        # Find student by email
        student = db.execute(
            f"SELECT students.id, students.email, users.username FROM students JOIN users ON students.id = users.student_id WHERE students.email = {placeholder}",
            (email,)
        ).fetchone()
        
        if student:
            # Generate token
            s = URLSafeTimedSerializer(app.config["SECRET_KEY"])
            token = s.dumps(email, salt="password-reset-salt")
            
            # Send reset email
            reset_url = url_for("reset_password", token=token, _external=True)
            subject = "Password Reset Request - Attendance System"
            message = (
                f"Hello,\n\n"
                f"A password reset was requested for your student account (Enrollment: {student['username']}).\n"
                f"Click the link below to reset your password:\n\n"
                f"{reset_url}\n\n"
                "If you did not request this, please ignore this email.\n"
                "The link will expire in 1 hour.\n\n"
                "Best regards,\n"
                "Attendance Management Team"
            )
            
            success, error = send_email(subject, email, message)
            if success:
                flash("A password reset link has been sent to your email.", "success")
            else:
                flash(f"Error sending email: {error}", "error")
        else:
            flash("No account found with that email address.", "error")
        
        db.close()
        return redirect(url_for("forgot_password"))

    return render_template("forgot_password.html")


@app.route("/reset_password/<token>", methods=["GET", "POST"])
def reset_password(token):
    s = URLSafeTimedSerializer(app.config["SECRET_KEY"])
    try:
        email = s.loads(token, salt="password-reset-salt", max_age=3600)
    except (SignatureExpired, BadSignature):
        flash("The password reset link is invalid or has expired.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        new_password = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()
        
        if not new_password or len(new_password) < 4:
            flash("Password must be at least 4 characters long.", "error")
        elif new_password != confirm_password:
            flash("Passwords do not match.", "error")
        else:
            db = get_db()
            placeholder = get_placeholder()
            
            # Update password in users table
            hashed_password = generate_password_hash(new_password)
            db.execute(
                f"UPDATE users SET password = {placeholder} WHERE student_id IN (SELECT id FROM students WHERE email = {placeholder})",
                (hashed_password, email)
            )
            db.commit()
            db.close()
            
            flash("Your password has been reset successfully. You can now login.", "success")
            return redirect(url_for("student_login"))

    return render_template("reset_password.html", token=token)



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

            emailed_students = notify_low_attendance(db, saved_student_ids, subject_id)
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
        mail_configured=bool(app.config["MAIL_USERNAME"] and app.config["MAIL_PASSWORD"]),
    )

@app.route("/api/generate_qr_token", methods=["GET"])
@login_required
def generate_qr_token():
    branch_id = request.args.get("branch_id")
    subject_id = request.args.get("subject_id")
    date_str = request.args.get("date")

    if not branch_id or not subject_id or not date_str:
        return abort(400, "Missing parameters")

    s = URLSafeTimedSerializer(app.config["SECRET_KEY"])
    token = s.dumps({"branch_id": branch_id, "subject_id": subject_id, "date": date_str})
    
    return jsonify({"token": token, "scan_url": url_for("scan_qr", token=token, _external=True)})

@app.route("/generate_qr")
@login_required
def generate_qr():
    branch_id = request.args.get("branch_id")
    subject_id = request.args.get("subject_id")
    date_str = request.args.get("date")
    
    if not branch_id or not subject_id or not date_str:
        flash("Missing parameters for QR generation", "error")
        return redirect(url_for("mark_attendance"))
        
    db = get_db()
    placeholder = get_placeholder()
    branch = db.execute(f"SELECT name FROM branches WHERE id = {placeholder}", (branch_id,)).fetchone()
    subject = db.execute(f"SELECT name FROM subjects WHERE id = {placeholder}", (subject_id,)).fetchone()
    db.close()
    
    return render_template(
        "qr_display.html",
        branch_id=branch_id,
        subject_id=subject_id,
        date=date_str,
        branch_name=branch["name"] if branch else "",
        subject_name=subject["name"] if subject else "",
    )

@app.route("/scan_qr")
def scan_qr():
    token = request.args.get("token")
    if not token:
        flash("Invalid or missing QR token.", "error")
        return redirect(url_for("index"))

    # Enforce student login
    if session.get("role") != "student":
        flash("You must be logged in as a student to scan QR codes.", "error")
        return redirect(url_for("student_login", next=request.url))

    s = URLSafeTimedSerializer(app.config["SECRET_KEY"])
    try:
        # Token valid for 5 minutes (300 seconds)
        data = s.loads(token, max_age=300)
    except SignatureExpired:
        flash("This QR code has expired. Please ask the teacher to generate a new one.", "error")
        return redirect(url_for("student_dashboard"))
    except BadSignature:
        flash("Invalid QR code.", "error")
        return redirect(url_for("student_dashboard"))

    branch_id = data.get("branch_id")
    subject_id = data.get("subject_id")
    date_str = data.get("date")
    student_id = session.get("student_id")

    db = get_db()
    placeholder = get_placeholder()
    
    # Check if student belongs to this branch
    student = db.execute(f"SELECT branch_id FROM students WHERE id = {placeholder}", (student_id,)).fetchone()
    if not student or str(student["branch_id"]) != str(branch_id):
        db.close()
        flash("You are not enrolled in this branch.", "error")
        return redirect(url_for("student_dashboard"))

    # Mark attendance as present
    existing = db.execute(
        f"SELECT id FROM attendance WHERE student_id = {placeholder} AND subject_id = {placeholder} AND date = {placeholder}",
        (student_id, subject_id, date_str),
    ).fetchone()

    if existing:
        db.execute(
            f"UPDATE attendance SET status = 'Present', note = 'QR Scan' WHERE id = {placeholder}",
            (existing["id"],),
        )
    else:
        db.execute(
            f"INSERT INTO attendance (student_id, branch_id, subject_id, date, status, note) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, 'Present', 'QR Scan')",
            (student_id, branch_id, subject_id, date_str),
        )
    
    db.commit()
    db.close()

    flash("Attendance successfully marked via QR Code!", "success")
    return redirect(url_for("student_dashboard"))

@app.route("/department_dashboard")
@login_required
def department_dashboard():
    db = get_db()
    placeholder = get_placeholder()

    # Fetch all branches (departments)
    branches = db.execute("SELECT * FROM branches ORDER BY name").fetchall()

    departments = []
    total_students = 0
    total_subjects = 0
    total_attendance = 0
    total_present = 0

    for branch in branches:
        bid = branch["id"]

        # Student count
        student_count = db.execute(
            f"SELECT COUNT(*) AS count FROM students WHERE branch_id = {placeholder}", (bid,)
        ).fetchone()["count"]

        # Subject count
        subject_count = db.execute(
            f"SELECT COUNT(*) AS count FROM subjects WHERE branch_id = {placeholder}", (bid,)
        ).fetchone()["count"]

        # Attendance counts
        att_stats = db.execute(
            f"""SELECT
                COUNT(*) AS total,
                COUNT(CASE WHEN status = 'Present' THEN 1 END) AS present
            FROM attendance WHERE branch_id = {placeholder}""",
            (bid,),
        ).fetchone()
        att_total = att_stats["total"] or 0
        att_present = att_stats["present"] or 0
        att_absent = att_total - att_present
        att_pct = round((att_present / att_total) * 100, 1) if att_total > 0 else 0

        # Subject-wise attendance
        subject_rows = db.execute(
            f"""SELECT subjects.name,
                COUNT(*) AS total,
                COUNT(CASE WHEN attendance.status = 'Present' THEN 1 END) AS present
            FROM attendance
            JOIN subjects ON attendance.subject_id = subjects.id
            WHERE attendance.branch_id = {placeholder}
            GROUP BY subjects.id, subjects.name
            ORDER BY subjects.name""",
            (bid,),
        ).fetchall()

        subjects_data = []
        for s in subject_rows:
            s_total = s["total"] or 0
            s_present = s["present"] or 0
            s_pct = round((s_present / s_total) * 100, 1) if s_total > 0 else 0
            subjects_data.append({
                "name": s["name"],
                "total": s_total,
                "present": s_present,
                "absent": s_total - s_present,
                "pct": s_pct,
            })

        # Student-wise attendance
        student_rows = db.execute(
            f"""SELECT students.id, students.name, students.enrollment, students.email,
                COUNT(attendance.id) AS total,
                COUNT(CASE WHEN attendance.status = 'Present' THEN 1 END) AS present
            FROM students
            LEFT JOIN attendance ON attendance.student_id = students.id
            WHERE students.branch_id = {placeholder}
            GROUP BY students.id, students.name, students.enrollment, students.email
            ORDER BY students.name""",
            (bid,),
        ).fetchall()

        students_data = []
        for st in student_rows:
            st_total = st["total"] or 0
            st_present = st["present"] or 0
            st_pct = round((st_present / st_total) * 100, 1) if st_total > 0 else 0
            students_data.append({
                "id": st["id"],
                "name": st["name"],
                "enrollment": st["enrollment"],
                "email": st["email"],
                "total": st_total,
                "present": st_present,
                "absent": st_total - st_present,
                "pct": st_pct,
            })

        departments.append({
            "id": bid,
            "name": branch["name"],
            "location": branch["location"],
            "student_count": student_count,
            "subject_count": subject_count,
            "attendance_count": att_total,
            "present_count": att_present,
            "absent_count": att_absent,
            "attendance_pct": att_pct,
            "subjects": subjects_data,
            "students": students_data,
        })

        total_students += student_count
        total_subjects += subject_count
        total_attendance += att_total
        total_present += att_present

    overall_percentage = round((total_present / total_attendance) * 100, 1) if total_attendance > 0 else 0

    # Fetch attendance trend for charts (last 7 days)
    chart_data = []
    for i in range(6, -1, -1):
        target_date = (date.today() - timedelta(days=i)).isoformat()
        day_stats = db.execute(f"""
            SELECT 
                COUNT(CASE WHEN status='Present' THEN 1 END) as present_count,
                COUNT(*) as total_count
            FROM attendance 
            WHERE date = {placeholder}
        """, (target_date,)).fetchone()
        
        pct = round((day_stats["present_count"] / day_stats["total_count"] * 100), 1) if day_stats["total_count"] > 0 else 0
        chart_data.append({"date": target_date, "percentage": pct})

    # Mail configuration info
    mail_info = {
        "configured": bool(app.config.get("MAIL_USERNAME")),
        "server": app.config.get("MAIL_SERVER"),
        "port": app.config.get("MAIL_PORT"),
        "username": app.config.get("MAIL_USERNAME"),
        "tls": app.config.get("MAIL_USE_TLS"),
        "render_env": "RENDER" in os.environ
    }

    persistence_warning = False
    if "RENDER" in os.environ and "sqlite" in app.config["DATABASE"].lower():
        persistence_warning = True

    database_info = {
        "storage": "PostgreSQL (Cloud)" if "postgresql" in app.config["DATABASE"].lower() else "SQLite (Local/Ephemeral)",
        "path": app.config["DATABASE"].split("@")[-1] if "@" in app.config["DATABASE"] else app.config["DATABASE"]
    }

    db.close()
    return render_template(
        "department_dashboard.html",
        departments=departments,
        total_students=total_students,
        total_subjects=total_subjects,
        total_attendance=total_attendance,
        overall_percentage=overall_percentage,
        persistence_warning=persistence_warning,
        database_info=database_info,
        chart_data=chart_data,
        mail_info=mail_info
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

@app.route("/export/excel")
@login_required
def export_excel():
    db = get_db()
    query = """
        SELECT 
            attendance.date, 
            students.name AS Student, 
            students.enrollment AS Enrollment, 
            branches.name AS Branch, 
            subjects.name AS Subject, 
            attendance.status AS Status,
            attendance.note AS Note
        FROM attendance 
        JOIN students ON attendance.student_id = students.id 
        JOIN branches ON attendance.branch_id = branches.id 
        JOIN subjects ON attendance.subject_id = subjects.id
        ORDER BY attendance.date DESC
    """
    records = db.execute(query).fetchall()
    db.close()
    
    if not records:
        flash("No records to export.", "error")
        return redirect(url_for("attendance_report"))

    df = pd.DataFrame([dict(r) for r in records])
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Attendance')
    
    output.seek(0)
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"Attendance_Report_{date.today().isoformat()}.xlsx"
    )

@app.route("/report/email", methods=["POST"])
@login_required
def report_email():
    branch_id = request.form.get("branch_id")
    subject_id = request.form.get("subject_id")
    
    db = get_db()
    placeholder = get_placeholder()
    
    query = """
        SELECT 
            students.name, 
            students.enrollment,
            COUNT(*) as total,
            COUNT(CASE WHEN status='Present' THEN 1 END) as present
        FROM attendance
        JOIN students ON attendance.student_id = students.id
        WHERE 1=1
    """
    params = []
    if branch_id:
        query += f" AND attendance.branch_id = {placeholder}"
        params.append(branch_id)
    if subject_id:
        query += f" AND attendance.subject_id = {placeholder}"
        params.append(subject_id)
    
    query += " GROUP BY students.id, students.name, students.enrollment"
    
    records = db.execute(query, params).fetchall()
    db.close()
    
    if not records:
        flash("No data found for this report.", "error")
        return redirect(url_for("attendance_report"))

    subject = "Attendance Report Summary"
    message = f"Attendance Report Summary ({date.today().isoformat()})\n\n"
    message += f"{'Name':<25} {'Enrollment':<15} {'Attended':<10} {'Total':<10} {'%'}\n"
    message += "-" * 70 + "\n"
    
    for r in records:
        pct = round((r['present'] / r['total'] * 100), 1) if r['total'] > 0 else 0
        message += f"{r['name']:<25} {r['enrollment']:<15} {r['present']:<10} {r['total']:<10} {pct}%\n"
    
    user_email = session.get("email") or app.config.get("MAIL_USERNAME")
    if send_email(subject, user_email, message):
        flash(f"Report sent to {user_email}", "success")
    else:
        flash("Failed to send email. Check mail configuration.", "error")
        
    return redirect(url_for("attendance_report"))


@app.route("/admin/import_data", methods=["POST"])
@login_required
def import_data():
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        db = get_db()
        placeholder = get_placeholder()

        # Import Branches
        for branch in data.get("branches", []):
            try:
                db.execute(
                    f"INSERT INTO branches (id, name, location) VALUES ({placeholder}, {placeholder}, {placeholder}) "
                    f"ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, location = EXCLUDED.location",
                    (branch["id"], branch["name"], branch["location"])
                )
            except Exception as e:
                print(f"Error importing branch {branch['name']}: {e}")

        # Import Subjects
        for subj in data.get("subjects", []):
            try:
                # Use standard INSERT for SQLite fallback, or ON CONFLICT for Postgres
                if app.config["DATABASE"].startswith("postgres"):
                    db.execute(
                        f"INSERT INTO subjects (id, name, branch_id) VALUES ({placeholder}, {placeholder}, {placeholder}) "
                        f"ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, branch_id = EXCLUDED.branch_id",
                        (subj["id"], subj["name"], subj["branch_id"])
                    )
                else:
                    db.execute(
                        f"INSERT OR REPLACE INTO subjects (id, name, branch_id) VALUES ({placeholder}, {placeholder}, {placeholder})",
                        (subj["id"], subj["name"], subj["branch_id"])
                    )
            except Exception as e:
                print(f"Error importing subject {subj['name']}: {e}")

        # Import Students
        for stu in data.get("students", []):
            try:
                if app.config["DATABASE"].startswith("postgres"):
                    db.execute(
                        f"INSERT INTO students (id, name, enrollment, branch_id, email) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}) "
                        f"ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, enrollment = EXCLUDED.enrollment, branch_id = EXCLUDED.branch_id, email = EXCLUDED.email",
                        (stu["id"], stu["name"], stu["enrollment"], stu["branch_id"], stu["email"])
                    )
                    # Also ensure user exists
                    db.execute(
                        f"INSERT INTO users (username, password, role, student_id) VALUES ({placeholder}, {placeholder}, 'student', {placeholder}) "
                        f"ON CONFLICT (username) DO NOTHING",
                        (stu["enrollment"], generate_password_hash(stu["enrollment"][-4:]), stu["id"])
                    )
                else:
                    db.execute(
                        f"INSERT OR REPLACE INTO students (id, name, enrollment, branch_id, email) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
                        (stu["id"], stu["name"], stu["enrollment"], stu["branch_id"], stu["email"])
                    )
                    db.execute(
                        f"INSERT OR IGNORE INTO users (username, password, role, student_id) VALUES ({placeholder}, {placeholder}, 'student', {placeholder})",
                        (stu["enrollment"], generate_password_hash(stu["enrollment"][-4:]), stu["id"])
                    )
            except Exception as e:
                print(f"Error importing student {stu['name']}: {e}")

        # Import Attendance
        for att in data.get("attendance", []):
            try:
                if app.config["DATABASE"].startswith("postgres"):
                    db.execute(
                        f"INSERT INTO attendance (id, student_id, branch_id, subject_id, date, status, note) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}) "
                        f"ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, note = EXCLUDED.note",
                        (att["id"], att["student_id"], att["branch_id"], att["subject_id"], att["date"], att["status"], att["note"])
                    )
                else:
                    db.execute(
                        f"INSERT OR REPLACE INTO attendance (id, student_id, branch_id, subject_id, date, status, note) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
                        (att["id"], att["student_id"], att["branch_id"], att["subject_id"], att["date"], att["status"], att["note"])
                    )
            except Exception as e:
                print(f"Error importing attendance record {att['id']}: {e}")

        db.commit()
        db.close()
        return jsonify({"message": "Data imported successfully!"}), 200
    except Exception as e:
        print(f"Import failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/test_email")
@login_required
def test_email():
    if session.get("role") != "admin":
        flash("Only admins can test email settings.", "error")
        return redirect(url_for("dashboard"))

    recipient = app.config["MAIL_FROM"]
    if not recipient:
        return "MAIL_FROM or MAIL_USERNAME not configured."

    subject = "Test Email from Attendance System"
    body = "This is a test email to verify your SMTP configuration. If you received this, your email settings are correct!"

    success, message = send_email(subject, recipient, body)

    if success:
        flash(f"Test email sent successfully to {recipient}!", "success")
    else:
        flash(f"Failed to send test email: {message}", "error")

    return redirect(url_for("settings"))


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
    db_uri = app.config["DATABASE"]
    is_pg = db_uri.startswith("postgresql") or db_uri.startswith("postgres")
    print(f"DATABASE CONNECTION: {'PostgreSQL' if is_pg else 'SQLite (WARNING: Ephemeral)'}")
    print(f"DATABASE URI: {db_uri[:20]}...") 
    
    try:
        init_db()
        print("Database initialization successful.")
    except Exception as e:
        print(f"Database initialization failed: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, debug=True)