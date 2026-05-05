import os
from datetime import date, timedelta
import sqlite3
import smtplib
import ssl
import time
import socket
from email.message import EmailMessage
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from flask import Flask, abort, redirect, render_template, request, session, url_for, flash, jsonify
from functools import wraps
from werkzeug.security import check_password_hash, generate_password_hash
# from flask_socketio import SocketIO, emit, join_room

# # Initialize SocketIO
# socketio = SocketIO(app, cors_allowed_origins="*")

app = Flask(__name__)
# Email sending is handled by the `send_email` helper defined later in the file.

# Use a stable SQLite file path relative to the application folder unless a PostgreSQL URL is provided.
db_env = os.environ.get("DATABASE_URL")
# Accept common postgres URL prefixes (postgres:// or postgresql://)
if db_env and db_env.startswith("postgres"):
    # Normalize to psycopg2-acceptable form if necessary
    if db_env.startswith("postgres://"):
        db_env = db_env.replace("postgres://", "postgresql://", 1)
    database_path = db_env
else:
    # Always use absolute path relative to app.py location
    app_dir = os.path.dirname(os.path.abspath(__file__))
    database_path = os.path.abspath(os.path.join(app_dir, "attendance.db"))
    print(f"App directory: {app_dir}")
    print(f"Using database path: {database_path}")
    print(f"Current working directory: {os.getcwd()}")

# Normalize SMTP credentials from environment to avoid accidental spaces/quotes
raw_mail_username = os.environ.get("MAIL_USERNAME") or ""
raw_mail_password = os.environ.get("MAIL_PASSWORD") or ""
# Trim surrounding whitespace and remove accidental inner spaces in passwords (common when copying)
mail_username = raw_mail_username.strip() if raw_mail_username else None
# Remove spaces commonly introduced when copying app-passwords (e.g. "abcd efgh ijkl mnop")
mail_password = raw_mail_password.strip().replace(" ", "") if raw_mail_password else None

app.config.from_mapping(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-key-change-in-production"),
    DATABASE=database_path,
    MAIL_SERVER=os.environ.get("MAIL_SERVER", "smtp.gmail.com"),
    MAIL_PORT=int(os.environ.get("MAIL_PORT", 587)),
    MAIL_USERNAME=mail_username,
    MAIL_PASSWORD=mail_password,
    MAIL_USE_TLS=os.environ.get("MAIL_USE_TLS", "True").lower() in ("true", "1", "yes"),
    MAIL_FROM=os.environ.get("MAIL_FROM", mail_username),
    LOW_ATTENDANCE_THRESHOLD=int(os.environ.get("LOW_ATTENDANCE_THRESHOLD", 75)),
)

def get_db():
    if str(app.config.get("DATABASE", "")).startswith("postgres"):
        import psycopg2
        from psycopg2.extras import RealDictCursor
        # psycopg2 accepts a postgres(ql) URL directly
        conn = psycopg2.connect(app.config["DATABASE"])
        conn.cursor_factory = RealDictCursor
        return conn
    else:
        conn = sqlite3.connect(app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        print(f"Database connection opened: {app.config['DATABASE']}")
        return conn

def get_placeholder():
    return "%s" if app.config["DATABASE"].startswith("postgresql") else "?"


def is_valid_email(email: str) -> bool:
    import re
    if not email:
        return False
    email = email.strip()
    # Simple RFC-5322-ish regex for common validation
    pattern = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
    return re.match(pattern, email) is not None


def is_mail_configured():
    username = app.config.get("MAIL_USERNAME")
    password = app.config.get("MAIL_PASSWORD")
    return bool(username and username.strip() and password and password.strip())


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
    # If credentials are not configured, allow a local dev fallback when explicitly enabled
    dev_fallback_enabled = os.environ.get("MAIL_DEV_FALLBACK", "False").lower() in ("1", "true", "yes")
    if not is_mail_configured():
        if not (dev_fallback_enabled or app.config.get("MAIL_SERVER") in ("localhost", "127.0.0.1")):
            print("Email not sent: mail credentials not configured.")
            return False
        print("Mail credentials not configured — attempting development fallback (unauthenticated SMTP).")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = app.config["MAIL_FROM"] or app.config["MAIL_USERNAME"]
    msg["To"] = recipient
    msg.set_content(body)
    # Resolve MAIL_SERVER to IPv4 address to avoid IPv6/errno 101 issues
    smtp_host = app.config.get("MAIL_SERVER")
    smtp_port = int(app.config.get("MAIL_PORT", 587))
    smtp_host_ip = None
    try:
        smtp_host_ip = socket.gethostbyname(smtp_host)
        print(f"Resolved SMTP host {smtp_host} -> {smtp_host_ip} (port {smtp_port})")
    except Exception as e:
        print(f"Warning: gethostbyname failed for {smtp_host}: {repr(e)}; will attempt to connect using hostname.")
        smtp_host_ip = smtp_host

    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            context = ssl.create_default_context()
            # Use explicit IPv4 connect to avoid IPv6 attempts
            if is_mail_configured():
                username = app.config.get("MAIL_USERNAME")
                password = app.config.get("MAIL_PASSWORD")
                if app.config.get("MAIL_USE_TLS"):
                    server = smtplib.SMTP(timeout=10)
                    # connect using IPv4 address
                    server.connect(smtp_host_ip, smtp_port)
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(username, password)
                    server.send_message(msg)
                    server.quit()
                else:
                    # SSL connection (no STARTTLS)
                    server = smtplib.SMTP_SSL(timeout=10, context=context)
                    server.connect(smtp_host_ip, smtp_port)
                    server.ehlo()
                    server.login(username, password)
                    server.send_message(msg)
                    server.quit()
            else:
                # Development fallback: unauthenticated send (MailHog on localhost)
                server = smtplib.SMTP(timeout=10)
                server.connect(smtp_host_ip, smtp_port)
                server.ehlo()
                server.send_message(msg)
                server.quit()

            print(f"Low attendance email sent to {recipient}")
            return True

        except OSError as e:
            print(f"Attempt {attempt} - OSError when sending email to {recipient}: {repr(e)}")
            try:
                local_addr = socket.gethostbyname(socket.gethostname())
                print(f"Local host resolves to {local_addr}")
            except Exception:
                pass
            if getattr(e, 'errno', None) == 101:
                print("Network is unreachable (errno 101). This is commonly caused by IPv6 routing issues or blocked outbound SMTP.\nEnsure the platform allows outbound SMTP or use an API-based transactional provider.")

        except smtplib.SMTPAuthenticationError as e:
            print(f"Attempt {attempt} - SMTP authentication failed for {recipient}: {repr(e)}")
            # Authentication problems are unlikely to be transient — break early
            return False

        except Exception as e:
            print(f"Attempt {attempt} - Failed to send email to {recipient}: {repr(e)}")

        if attempt < attempts:
            time.sleep(2)
            continue
        return False


def get_reset_serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="password-reset")


def generate_reset_token(user_id):
    serializer = get_reset_serializer()
    return serializer.dumps({"user_id": user_id})


def verify_reset_token(token, max_age=3600):
    serializer = get_reset_serializer()
    try:
        data = serializer.loads(token, max_age=max_age)
    except SignatureExpired:
        return None, "expired"
    except BadSignature:
        return None, "invalid"

    user_id = data.get("user_id")
    if not user_id:
        return None, "invalid"
    return user_id, None


def notify_low_attendance(db, student_ids):
    if not student_ids or not is_mail_configured():
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
            if send_email(
                subject=f"Low Attendance Alert: {row['percentage']}%",
                recipient=row["email"],
                body=body,
            ):
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
    branch_data = db.execute("""
        SELECT
            branches.name AS branch_name,
            branches.location AS location,
            COUNT(DISTINCT students.id) AS student_count,
            COUNT(DISTINCT subjects.id) AS subject_count,
            COUNT(attendance.id) AS attendance_count,
            ROUND(
                COUNT(CASE WHEN attendance.status='Present' THEN 1 END)*100.0 / NULLIF(COUNT(attendance.id),0),
                1
            ) AS attendance_percentage
        FROM branches
        LEFT JOIN students ON branches.id = students.branch_id
        LEFT JOIN subjects ON branches.id = subjects.branch_id
        LEFT JOIN attendance ON branches.id = attendance.branch_id
        GROUP BY branches.id, branches.name, branches.location
        ORDER BY branches.name
    """).fetchall()

    db.close()
    database_info = {
        "storage": "PostgreSQL" if app.config["DATABASE"].startswith("postgresql") else "SQLite",
        "path": app.config["DATABASE"],
    }
    mail_info = {
        "configured": is_mail_configured(),
        "server": app.config["MAIL_SERVER"],
        "port": app.config["MAIL_PORT"],
        "username": app.config["MAIL_USERNAME"],
        "tls": app.config["MAIL_USE_TLS"],
        "render_env": bool(os.environ.get("RENDER") or os.environ.get("RENDER_INTERNAL_HOSTNAME")),
    }

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
        "configured": is_mail_configured(),
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
            except Exception as e:
                db.rollback()
                print(f"Error adding branch: {e}")
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
            try:
                db.execute(
                    f"INSERT INTO subjects (name, branch_id) VALUES ({placeholder}, {placeholder})",
                    (name, branch_id),
                )
                db.commit()
                flash("Subject added successfully.", "success")
            except Exception as e:
                db.rollback()
                print(f"Error adding subject: {e}")
                flash("Error adding subject. Please try again.", "error")
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
        # Basic validation
        if not (name and enrollment and branch_id):
            flash("Student name, enrollment and branch are required.", "error")
        elif email and not is_valid_email(email):
            flash("Please enter a valid email address.", "error")
        else:
            # Check duplicates before attempting insert
            existing = db.execute(
                f"SELECT id FROM students WHERE enrollment = {placeholder}",
                (enrollment,),
            ).fetchone()
            if existing:
                flash("A student with this enrollment already exists.", "error")
            else:
                try:
                    if str(app.config.get("DATABASE", "")).startswith("postgres"):
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
                    db.rollback()
                    print(f"Error adding student: {e}")
                    flash("Enrollment or username already exists.", "error")

    students = db.execute(
        f"SELECT students.*, branches.name AS branch_name FROM students JOIN branches ON students.branch_id = branches.id ORDER BY students.name"
    ).fetchall()
    db.close()
    return render_template("students.html", students=students, branches=branches)


@app.route("/student_login", methods=["GET", "POST"])
def student_login():
    next_url = request.args.get("next") or request.form.get("next")

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        db = get_db()
        placeholder = get_placeholder()
        user = db.execute(f"SELECT * FROM users WHERE username = {placeholder}", (username,)).fetchone()
        db.close()

        if not username or not password:
            flash("Please enter username and password.", "error")
        elif user and user["role"] == "student" and check_password_hash(user["password"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            # sqlite row and psycopg2 RealDictCursor return different types; handle both
            try:
                student_id_val = user["student_id"]
            except Exception:
                student_id_val = user.get("student_id")
            session["student_id"] = student_id_val
            if next_url:
                return redirect(next_url)
            return redirect(url_for("student_dashboard"))

        flash("Invalid student login credentials.", "error")

    return render_template("student_login.html", next=next_url)


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

    selected_subject_id = request.args.get("subject_id") or ""

    subjects = db.execute(
        f"SELECT id, name FROM subjects WHERE branch_id = {placeholder} ORDER BY name",
        (student["branch_id"],),
    ).fetchall()

    attendance_query = (
        f"SELECT attendance.date, attendance.status, subjects.name AS subject_name, subjects.id AS subject_id "
        f"FROM attendance "
        f"JOIN subjects ON attendance.subject_id = subjects.id "
        f"WHERE attendance.student_id = {placeholder} "
    )
    params = [student_id]
    if selected_subject_id:
        attendance_query += f"AND attendance.subject_id = {placeholder} "
        params.append(selected_subject_id)
    attendance_query += "ORDER BY attendance.date DESC"

    attendance_records = db.execute(attendance_query, tuple(params)).fetchall()

    total = len(attendance_records)
    present = len([a for a in attendance_records if a["status"] == "Present"])
    percentage = round((present / total) * 100, 1) if total > 0 else 0

    db.close()
    return render_template(
        "student_dashboard.html",
        student=student,
        attendance_records=attendance_records,
        percentage=percentage,
        subjects=subjects,
        selected_subject_id=selected_subject_id,
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

    selected_subject_id = request.args.get("subject_id") or ""

    subjects = db.execute(
        f"SELECT id, name FROM subjects WHERE branch_id = {placeholder} ORDER BY name",
        (student["branch_id"],),
    ).fetchall()

    attendance_query = (
        f"SELECT attendance.date, attendance.status, subjects.name AS subject_name, subjects.id AS subject_id "
        f"FROM attendance "
        f"JOIN subjects ON attendance.subject_id = subjects.id "
        f"WHERE attendance.student_id = {placeholder} "
    )
    params = [student_id]
    if selected_subject_id:
        attendance_query += f"AND attendance.subject_id = {placeholder} "
        params.append(selected_subject_id)
    attendance_query += "ORDER BY attendance.date DESC"

    attendance_records = db.execute(attendance_query, tuple(params)).fetchall()

    total = len(attendance_records)
    present = len([a for a in attendance_records if a["status"] == "Present"])
    percentage = round((present / total) * 100, 1) if total > 0 else 0

    db.close()
    return render_template(
        "student_dashboard.html",
        student=student,
        attendance_records=attendance_records,
        percentage=percentage,
        subjects=subjects,
        selected_subject_id=selected_subject_id,
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
    today_date = date.today()
    try:
        selected_date_obj = date.fromisoformat(selected_date)
    except ValueError:
        selected_date_obj = today_date

    if selected_date_obj > today_date:
        selected_date_obj = today_date

    selected_date = selected_date_obj.isoformat()
    subjects = []
    students = []
    existing_dates = []

    # Calculate previous and next dates
    current_date_obj = selected_date_obj
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
        try:
            selected_date_obj = date.fromisoformat(selected_date)
        except ValueError:
            selected_date_obj = today_date

        if selected_date_obj > today_date:
            selected_date_obj = today_date

        selected_date = selected_date_obj.isoformat()
        student_ids = request.form.getlist("student_id")

        if branch_id and subject_id and student_ids:
            saved_student_ids = []
            try:
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
                print(f"Committed attendance for {len(saved_student_ids)} students on {selected_date}")
                
                # Verify the data was saved
                count = db.execute(
                    f"SELECT COUNT(*) FROM attendance WHERE date = {placeholder}",
                    (selected_date,)
                ).fetchone()[0]
                print(f"Total attendance records for {selected_date}: {count}")

                flash("Attendance saved successfully.", "success")
                emailed_students = notify_low_attendance(db, saved_student_ids)
                session["attendance_email_summary"] = emailed_students
            except Exception as e:
                db.rollback()
                print(f"Error saving attendance: {e}")
                flash("Error saving attendance. Please try again.", "error")
                saved_student_ids = []

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
        today_date=today_date.isoformat(),
    )


@app.route("/attendance/qr")
@login_required
def generate_qr():
    branch_id = request.args.get("branch_id")
    subject_id = request.args.get("subject_id")
    selected_date = request.args.get("date") or date.today().isoformat()

    if not branch_id or not subject_id:
        flash("Please select a branch and subject before generating a QR code.", "error")
        return redirect(url_for("mark_attendance", branch_id=branch_id, subject_id=subject_id, date=selected_date))

    db = get_db()
    placeholder = get_placeholder()
    branch = db.execute(
        f"SELECT name FROM branches WHERE id = {placeholder}",
        (branch_id,),
    ).fetchone()
    subject = db.execute(
        f"SELECT name FROM subjects WHERE id = {placeholder}",
        (subject_id,),
    ).fetchone()
    db.close()

    if not branch or not subject:
        flash("Selected branch or subject was not found.", "error")
        return redirect(url_for("mark_attendance", branch_id=branch_id, subject_id=subject_id, date=selected_date))

    return render_template(
        "qr_display.html",
        branch_id=branch_id,
        subject_id=subject_id,
        branch_name=branch["name"],
        subject_name=subject["name"],
        date=selected_date,
    )


@app.route("/attendance/scan")
def attendance_scan():
    branch_id = request.args.get("branch_id")
    subject_id = request.args.get("subject_id")
    selected_date = request.args.get("date") or date.today().isoformat()

    if not branch_id or not subject_id:
        flash("Invalid attendance scan link.", "error")
        return redirect(url_for("student_login"))

    if not session.get("user_id") or session.get("role") != "student":
        login_url = url_for("student_login", next=request.url)
        return redirect(login_url)

    student_id = session.get("student_id")
    if not student_id:
        flash("Student session not found. Please log in again.", "error")
        return redirect(url_for("student_login", next=request.url))

    db = get_db()
    placeholder = get_placeholder()
    branch = db.execute(
        f"SELECT name FROM branches WHERE id = {placeholder}",
        (branch_id,),
    ).fetchone()
    subject = db.execute(
        f"SELECT name FROM subjects WHERE id = {placeholder}",
        (subject_id,),
    ).fetchone()

    if not branch or not subject:
        db.close()
        flash("Attendance scan link is invalid.", "error")
        return redirect(url_for("student_dashboard"))

    existing = db.execute(
        f"SELECT id, status FROM attendance WHERE student_id = {placeholder} AND subject_id = {placeholder} AND date = {placeholder}",
        (student_id, subject_id, selected_date),
    ).fetchone()

    if existing:
        if existing["status"] != "Present":
            db.execute(
                f"UPDATE attendance SET status = {placeholder}, note = {placeholder} WHERE id = {placeholder}",
                ("Present", "Marked via QR scan", existing["id"]),
            )
            db.commit()
            message = "Your attendance has been updated to Present."
        else:
            message = "Your attendance is already marked as Present."
    else:
        db.execute(
            f"INSERT INTO attendance (student_id, branch_id, subject_id, date, status, note) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
            (student_id, branch_id, subject_id, selected_date, "Present", "Marked via QR scan"),
        )
        db.commit()
        message = "Attendance recorded successfully."

    db.close()

    return render_template(
        "attendance_scan.html",
        branch_name=branch["name"],
        subject_name=subject["name"],
        date=selected_date,
        message=message,
    )


@app.route("/api/generate_qr_token")
@login_required
def generate_qr_token():
    branch_id = request.args.get("branch_id")
    subject_id = request.args.get("subject_id")
    selected_date = request.args.get("date") or date.today().isoformat()

    if not branch_id or not subject_id:
        return jsonify({"error": "branch_id and subject_id are required."}), 400

    scan_url = url_for(
        "attendance_scan",
        branch_id=branch_id,
        subject_id=subject_id,
        date=selected_date,
        _external=True,
    )
    return jsonify({"scan_url": scan_url})


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
    mail_configured = is_mail_configured()

    return render_template(
        "attendance_success.html",
        branch_name=branch["name"] if branch else "",
        subject_name=subject["name"] if subject else "",
        selected_date=selected_date,
        attendance_count=attendance_count,
        email_summary=email_summary,
        mail_configured=mail_configured,
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

@app.route("/forgot-password", methods=["GET", "POST"])
@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        identifier = request.form.get("email", "").strip()

        if not identifier:
            flash("Please enter your registered email address or enrollment number.", "error")
            return render_template("forgot_password.html")

        if not is_mail_configured():
            flash("Email service is not configured. Contact the administrator.", "error")
            return render_template("forgot_password.html")

        db = get_db()
        placeholder = get_placeholder()

        # Allow students to submit either email or enrollment number
        student = None
        if "@" in identifier:
            student = db.execute(
                f"SELECT id, email FROM students WHERE LOWER(email) = LOWER({placeholder})",
                (identifier,),
            ).fetchone()
        else:
            student = db.execute(
                f"SELECT id, email FROM students WHERE LOWER(enrollment) = LOWER({placeholder})",
                (identifier,),
            ).fetchone()

        user = None
        if student:
            user = db.execute(
                f"SELECT id FROM users WHERE role = {placeholder} AND student_id = {placeholder}",
                ("student", student["id"]),
            ).fetchone()

        # Only send reset link if we found a user and the student has an email address configured
        if user and student and student.get("email"):
            token = generate_reset_token(user["id"])
            reset_link = url_for("reset_password", token=token, _external=True)
            body = (
                "Hello,\n\n"
                "We received a request to reset your password. Use the link below to set a new password:\n\n"
                f"{reset_link}\n\n"
                "If you did not request a reset, you can ignore this email.\n\n"
                "Regards,\n"
                "Attendance Management Team"
            )
            send_email(
                subject="Reset your password",
                recipient=student["email"],
                body=body,
            )
        elif student and (not student.get("email") or not student.get("email").strip()):
            # Found student but no email configured
            flash("Your account does not have an email address on file. Contact the administrator to reset your password.", "error")
            db.close()
            return render_template("forgot_password.html")

        db.close()
        flash("If this account exists and has an email, a reset link has been sent.", "success")

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
@app.route("/reset_password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user_id, error = verify_reset_token(token)
    if error:
        flash("Reset link is invalid or expired. Please request a new one.", "error")
        return redirect(url_for("forgot_password"))

    db = get_db()
    placeholder = get_placeholder()
    user = db.execute(
        f"SELECT id, role FROM users WHERE id = {placeholder}",
        (user_id,),
    ).fetchone()

    if not user or user["role"] != "student":
        db.close()
        flash("Reset link is invalid or expired. Please request a new one.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        if not password or not confirm_password:
            flash("Please enter and confirm your new password.", "error")
        elif password != confirm_password:
            flash("Passwords do not match.", "error")
        else:
            db.execute(
                f"UPDATE users SET password = {placeholder} WHERE id = {placeholder}",
                (generate_password_hash(password), user_id),
            )
            db.commit()
            db.close()
            flash("Password updated successfully. Please log in.", "success")
            return redirect(url_for("student_login"))

    db.close()
    return render_template("reset_password.html")


@app.route('/admin/check-smtp')
@login_required
def admin_check_smtp():
    # Only admins may run this check
    if session.get('role') != 'admin':
        abort(403)

    host = app.config.get('MAIL_SERVER')
    port = int(app.config.get('MAIL_PORT', 587))
    timeout = 8
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return jsonify({'ok': True, 'server': host, 'port': port, 'message': 'Connection successful'})
    except Exception as e:
        return jsonify({'ok': False, 'server': host, 'port': port, 'error': str(e)})
with app.app_context():
    try:
        print(f"Database path: {app.config['DATABASE']}")
        print(f"Database file exists: {os.path.exists(app.config['DATABASE'])}")
        if os.path.exists(app.config['DATABASE']):
            db_size = os.path.getsize(app.config['DATABASE'])
            print(f"Database file size: {db_size} bytes")
        init_db()
        print("Database initialized successfully")
    except Exception as e:
        print(f"Database initialization failed: {e}")
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
