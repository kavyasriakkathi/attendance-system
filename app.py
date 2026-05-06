import os
from datetime import date, timedelta
from io import BytesIO
import sqlite3
import smtplib
import ssl
import time
import socket
import traceback
from urllib.parse import urlparse
from email.message import EmailMessage
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from flask import Flask, abort, redirect, render_template, request, session, url_for, flash, jsonify, send_file
from functools import wraps
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename
# from flask_socketio import SocketIO, emit, join_room

# # Initialize SocketIO
# socketio = SocketIO(app, cors_allowed_origins="*")

app = Flask(__name__)
# Email sending is handled by the `send_email` helper defined later in the file.


# One-time schema init guard (per process). This prevents a missing-table crash
# after switching from SQLite to PostgreSQL, while keeping overhead low.
_DB_INIT_DONE = False
_DB_INIT_LAST_ERROR = None


@app.errorhandler(Exception)
def log_unhandled_exception(e):
    """Log a full traceback to Render logs for any unexpected 500.

    Note: HTTPException (404/403/etc) should be handled by Flask normally.
    """
    if isinstance(e, HTTPException):
        return e

    try:
        print(f"[ERROR] Unhandled exception type={type(e).__name__} on {request.method} {request.path}")
        print(f"[ERROR] message={repr(e)}")
        # Avoid logging sensitive fields
        if request.method in ("POST", "PUT", "PATCH"):
            safe_form = {k: ("<hidden>" if k.lower() in ("password", "confirm_password") else v) for k, v in request.form.items()}
            print(f"[ERROR] form={safe_form}")
        print(traceback.format_exc())
    except Exception:
        # Never crash while trying to log
        pass

    return "Internal Server Error", 500

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
    REPORT_ADMIN_EMAIL=os.environ.get("REPORT_ADMIN_EMAIL", "instituteattendanceapp@gmail.com"),
    LOW_ATTENDANCE_THRESHOLD=int(os.environ.get("LOW_ATTENDANCE_THRESHOLD", 75)),
)

def get_db():
    db_url = str(app.config.get("DATABASE", ""))
    if db_url.startswith("postgres"):
        try:
            import psycopg2
            from psycopg2.extras import DictCursor
        except Exception as e:
            # If psycopg2 isn't installed in the environment, fail loudly with context.
            # (This often shows up as "Internal Server Error" on Render.)
            print("[DB] psycopg2 import failed. Is psycopg2-binary in requirements.txt?")
            print(f"[DB] import_error={repr(e)}")
            raise

        def _ensure_sslmode(url: str) -> str:
            """Render Postgres commonly requires SSL. Add sslmode=require if missing."""
            # If user already provided sslmode in DATABASE_URL, keep it.
            if "sslmode=" in url:
                return url
            # Only force SSL automatically on Render.
            is_render = bool(os.environ.get("RENDER") or os.environ.get("RENDER_INTERNAL_HOSTNAME"))
            if not is_render:
                return url
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}sslmode=require"

        safe_db_url = _ensure_sslmode(db_url)

        class _PostgresDB:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, query, params=()):
                # DictCursor returns DictRow which supports both index and key access
                # (closer to sqlite3.Row behavior used throughout this codebase).
                cur = self._conn.cursor(cursor_factory=DictCursor)
                cur.execute(query, params)
                return cur

            def commit(self):
                return self._conn.commit()

            def rollback(self):
                return self._conn.rollback()

            def close(self):
                return self._conn.close()

        try:
            # connect_timeout prevents requests hanging forever during outages.
            conn = psycopg2.connect(safe_db_url, connect_timeout=8)
        except Exception as e:
            # Log a short, non-secret connection summary for Render.
            try:
                parsed = urlparse(db_url)
                print(
                    "[DB] PostgreSQL connection failed "
                    f"host={parsed.hostname} port={parsed.port} db={parsed.path.lstrip('/')}"
                )
            except Exception:
                print("[DB] PostgreSQL connection failed (unable to parse DATABASE_URL)")
            print(f"[DB] error={repr(e)}")
            raise
        db = _PostgresDB(conn)

        # Ensure schema exists (safe to call multiple times, but we guard for speed).
        try:
            ensure_db_initialized(db)
        except Exception:
            # Initialization failure should not crash every request handler.
            # Individual routes will handle missing-table errors with user-friendly flashes.
            pass

        return db
    else:
        conn = sqlite3.connect(app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        print(f"Database connection opened: {app.config['DATABASE']}")

        # SQLite schema init (no-op if already created)
        try:
            ensure_db_initialized(conn)
        except Exception:
            pass

        return conn


def ensure_db_initialized(db) -> bool:
    """Create tables + default users/settings once per process.

    This prevents login routes from crashing with UndefinedTable errors when the
    Postgres database is new or has been recently replaced.
    """
    global _DB_INIT_DONE, _DB_INIT_LAST_ERROR
    if _DB_INIT_DONE:
        return True
    try:
        init_db(db=db)
        _DB_INIT_DONE = True
        _DB_INIT_LAST_ERROR = None
        return True
    except Exception as e:
        _DB_INIT_LAST_ERROR = repr(e)
        print(f"[DB] init_db failed: {_DB_INIT_LAST_ERROR}")
        print(traceback.format_exc())
        return False

def get_placeholder():
    return "%s" if str(app.config.get("DATABASE", "")).startswith("postgres") else "?"


def row_get(row, key, default=None):
    """Access a column from either sqlite3.Row or dict-like (psycopg2 RealDictRow)."""
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        try:
            return row.get(key, default)
        except Exception:
            return default


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


def send_email(subject, recipient, body, attachments=None):
    # If credentials are not configured, allow a local dev fallback when explicitly enabled
    dev_fallback_enabled = os.environ.get("MAIL_DEV_FALLBACK", "False").lower() in ("1", "true", "yes")
    if not is_mail_configured():
        if not (dev_fallback_enabled or app.config.get("MAIL_SERVER") in ("localhost", "127.0.0.1")):
            print("Email not sent: mail credentials not configured.")
            return False
        print("Mail credentials not configured — attempting development fallback (unauthenticated SMTP).")
    # Build message defensively so this helper never crashes a request handler.
    try:
        if not recipient or not str(recipient).strip():
            print("Email not sent: recipient is empty.")
            return False
        from_addr = (app.config.get("MAIL_FROM") or app.config.get("MAIL_USERNAME") or "").strip()
        if not from_addr:
            print("Email not sent: MAIL_FROM/MAIL_USERNAME is empty.")
            return False

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = recipient
        msg.set_content(body)

        if attachments:
            for attachment in attachments:
                filename = (attachment.get("filename") or "attachment").strip()
                content = attachment.get("content", b"")
                if isinstance(content, str):
                    content = content.encode("utf-8")
                mimetype = attachment.get("mimetype") or "application/octet-stream"
                if "/" in mimetype:
                    maintype, subtype = mimetype.split("/", 1)
                else:
                    maintype, subtype = "application", "octet-stream"
                msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    except Exception as e:
        print(f"Email not sent: failed to build message: {repr(e)}")
        return False
    smtp_host = app.config.get("MAIL_SERVER")
    smtp_port = int(app.config.get("MAIL_PORT", 587))
    use_tls = bool(app.config.get("MAIL_USE_TLS"))
    debug = os.environ.get("MAIL_DEBUG", "False").lower() in ("1", "true", "yes")

    def _try_send(host_to_use: str) -> None:
        context = ssl.create_default_context()
        if is_mail_configured():
            username = app.config.get("MAIL_USERNAME")
            password = app.config.get("MAIL_PASSWORD")
            if use_tls:
                with smtplib.SMTP(host_to_use, smtp_port, timeout=10) as server:
                    if debug:
                        server.set_debuglevel(1)
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(username, password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP_SSL(host_to_use, smtp_port, context=context, timeout=10) as server:
                    if debug:
                        server.set_debuglevel(1)
                    server.ehlo()
                    server.login(username, password)
                    server.send_message(msg)
        else:
            # Development fallback: unauthenticated SMTP (MailHog/smtp4dev)
            with smtplib.SMTP(host_to_use, smtp_port, timeout=10) as server:
                if debug:
                    server.set_debuglevel(1)
                server.ehlo()
                server.send_message(msg)

    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            # 1) Normal simple connection (beginner-friendly)
            _try_send(smtp_host)
            print(f"Email sent to {recipient}")
            return True

        except smtplib.SMTPAuthenticationError as e:
            print(f"Attempt {attempt} - SMTP authentication failed: {repr(e)}")
            return False

        except OSError as e:
            print(f"Attempt {attempt} - OSError when sending email to {recipient}: {repr(e)}")
            if getattr(e, "errno", None) == 101:
                # 2) Retry once using IPv4 A-record (avoids common IPv6 routing issues)
                try:
                    ipv4 = socket.gethostbyname(smtp_host)
                    print(f"Retrying with IPv4 address: {smtp_host} -> {ipv4}")
                    _try_send(ipv4)
                    print(f"Email sent to {recipient}")
                    return True
                except Exception as retry_err:
                    print(f"IPv4 retry failed: {repr(retry_err)}")

        except Exception as e:
            print(f"Attempt {attempt} - Failed to send email to {recipient}: {repr(e)}")

        if attempt < attempts:
            time.sleep(2)
            continue
        return False


def safe_send_email(subject: str, recipient: str, body: str, attachments=None) -> bool:
    """Wrapper around send_email() that guarantees no exception escapes."""
    try:
        return bool(send_email(subject=subject, recipient=recipient, body=body, attachments=attachments))
    except Exception as e:
        print(f"safe_send_email: unexpected error: {repr(e)}")
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


def init_db(db=None):
    """Create schema and seed minimal defaults.

    If `db` is provided, it must support .execute(), .commit(), and optionally
    .rollback(). The connection will NOT be closed by this function.
    """
    created_here = False
    if db is None:
        db = get_db()
        created_here = True
    placeholder = get_placeholder()

    # ✅ Create tables
    if str(app.config.get("DATABASE", "")).startswith("postgres"):
        # PostgreSQL specific
        # IMPORTANT: create referenced tables before tables with FOREIGN KEYs.

        db.execute("""
        CREATE TABLE IF NOT EXISTS branches (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            location TEXT
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
        CREATE TABLE IF NOT EXISTS subjects (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            branch_id INTEGER NOT NULL
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
    if created_here:
        try:
            db.close()
        except Exception:
            pass
def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return view(**kwargs)
    return wrapped_view
@app.route("/login", methods=["GET", "POST"])
@app.route("/admin_login", methods=["GET", "POST"])  # compatibility URL
@app.route("/admin-login", methods=["GET", "POST"])  # compatibility URL
def login():
    if request.method == "POST":
        username = (request.form.get("username", "") or "").strip()
        password = (request.form.get("password", "") or "").strip()

        if not username or not password:
            flash("Please enter username and password.", "error")
            return render_template("login.html")

        db = None
        try:
            db = get_db()
            placeholder = get_placeholder()
            user = db.execute(
                f"SELECT id, username, password, role FROM users WHERE username = {placeholder}",
                (username,),
            ).fetchone()

            if user and check_password_hash(row_get(user, "password"), password):
                if row_get(user, "role") == "student":
                    flash("Please use the student login page.", "error")
                    return redirect(url_for("student_login"))

                session.clear()
                session["user_id"] = row_get(user, "id")
                session["username"] = row_get(user, "username")
                session["role"] = row_get(user, "role")
                return redirect(url_for("dashboard"))

            flash("Invalid username or password.", "error")

        except Exception as e:
            print(f"[login] ERROR: {repr(e)}")
            print(traceback.format_exc())
            # Common after migrating to PostgreSQL: tables missing or bad DATABASE_URL.
            flash("Login is temporarily unavailable (database error). Please try again in a minute.", "error")
        finally:
            try:
                if db is not None:
                    db.close()
            except Exception:
                pass

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

    total_classes = 0
    present_count = 0
    absent_count = 0
    try:
        total_classes = int(row_get(attendance_stats, "total_count") or 0)
        present_count = int(row_get(attendance_stats, "present_count") or 0)
        absent_count = max(total_classes - present_count, 0)
    except Exception:
        total_classes = 0
        present_count = 0
        absent_count = 0

    overall_percentage = 0
    if total_classes > 0:
        overall_percentage = round(
            (present_count / total_classes) * 100, 1
        )

    subject_data = db.execute(
        """
        SELECT
            subjects.name AS name,
            SUM(CASE WHEN attendance.status='Present' THEN 1 ELSE 0 END) AS present_count,
            COUNT(*) AS total_count,
            ROUND(
                SUM(CASE WHEN attendance.status='Present' THEN 1 ELSE 0 END)*100.0 / NULLIF(COUNT(*), 0),
                1
            ) AS percentage
        FROM attendance
        JOIN subjects ON attendance.subject_id = subjects.id
        GROUP BY subjects.id, subjects.name
        ORDER BY subjects.name
        """
    ).fetchall()

    subject_chart_labels = [row_get(r, "name") for r in subject_data]
    subject_chart_percentages = [float(row_get(r, "percentage") or 0) for r in subject_data]
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

    # Build last-7-days chart data for the dashboard
    chart_dates = [date.today() - timedelta(days=i) for i in range(6, -1, -1)]
    chart_date_values = [d.isoformat() for d in chart_dates]
    chart_data = []
    if chart_date_values:
        placeholder = get_placeholder()
        chart_rows = db.execute(
            f"""
            SELECT
                date,
                SUM(CASE WHEN status='Present' THEN 1 ELSE 0 END) AS present_count,
                COUNT(*) AS total_count
            FROM attendance
            WHERE date IN ({', '.join([placeholder] * len(chart_date_values))})
            GROUP BY date
            """,
            tuple(chart_date_values),
        ).fetchall()
        chart_map = {row_get(r, "date"): r for r in chart_rows}
        for date_str in chart_date_values:
            row = chart_map.get(date_str)
            total_count = row_get(row, "total_count", 0) or 0
            present_count = row_get(row, "present_count", 0) or 0
            percentage = round((present_count / total_count) * 100, 1) if total_count else 0
            chart_data.append({"date": date_str, "percentage": percentage})

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
        total_classes=total_classes,
        present_count=present_count,
        absent_count=absent_count,
        overall_percentage=overall_percentage,
        subject_data=subject_data,
        subject_chart_labels=subject_chart_labels,
        subject_chart_percentages=subject_chart_percentages,
        branch_data=branch_data,
        chart_data=chart_data,
        database_info=database_info,
        mail_info=mail_info,
    )


@app.route("/department-dashboard")
@login_required
def department_dashboard():
    db = get_db()

    departments = db.execute(
        "SELECT id, name, location FROM branches ORDER BY name"
    ).fetchall()

    total_students = db.execute("SELECT COUNT(*) AS count FROM students").fetchone()
    total_subjects = db.execute("SELECT COUNT(*) AS count FROM subjects").fetchone()
    total_attendance = db.execute("SELECT COUNT(*) AS count FROM attendance").fetchone()

    attendance_stats = db.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'Present' THEN 1 ELSE 0 END) AS present_count,
            COUNT(*) AS total_count
        FROM attendance
        """
    ).fetchone()

    overall_percentage = 0
    total_count = row_get(attendance_stats, "total_count", 0) or 0
    present_count = row_get(attendance_stats, "present_count", 0) or 0
    if total_count:
        overall_percentage = round((present_count / total_count) * 100, 1)

    student_counts = {
        row_get(row, "branch_id"): row_get(row, "count", 0) or 0
        for row in db.execute(
            "SELECT branch_id, COUNT(*) AS count FROM students GROUP BY branch_id"
        ).fetchall()
    }

    subject_counts = {
        row_get(row, "branch_id"): row_get(row, "count", 0) or 0
        for row in db.execute(
            "SELECT branch_id, COUNT(*) AS count FROM subjects GROUP BY branch_id"
        ).fetchall()
    }

    attendance_counts = {}
    present_counts = {}
    absent_counts = {}
    for row in db.execute(
        """
        SELECT
            branch_id,
            SUM(CASE WHEN status = 'Present' THEN 1 ELSE 0 END) AS present_count,
            SUM(CASE WHEN status = 'Absent' THEN 1 ELSE 0 END) AS absent_count,
            COUNT(*) AS total_count
        FROM attendance
        GROUP BY branch_id
        """
    ).fetchall():
        branch_id = row_get(row, "branch_id")
        present_counts[branch_id] = row_get(row, "present_count", 0) or 0
        absent_counts[branch_id] = row_get(row, "absent_count", 0) or 0
        attendance_counts[branch_id] = row_get(row, "total_count", 0) or 0

    subjects_by_branch = {}
    for row in db.execute(
        """
        SELECT
            subjects.id AS subject_id,
            subjects.branch_id AS branch_id,
            subjects.name AS subject_name,
            SUM(CASE WHEN attendance.status = 'Present' THEN 1 ELSE 0 END) AS present_count,
            COUNT(attendance.id) AS total_count
        FROM subjects
        LEFT JOIN attendance ON attendance.subject_id = subjects.id
        GROUP BY subjects.id, subjects.branch_id, subjects.name
        ORDER BY subjects.name
        """
    ).fetchall():
        branch_id = row_get(row, "branch_id")
        total = row_get(row, "total_count", 0) or 0
        present = row_get(row, "present_count", 0) or 0
        pct = round((present / total) * 100, 1) if total else 0
        subjects_by_branch.setdefault(branch_id, []).append(
            {
                "id": row_get(row, "subject_id"),
                "name": row_get(row, "subject_name"),
                "present_count": present,
                "total_count": total,
                "pct": pct,
            }
        )

    students_by_branch = {}
    for row in db.execute(
        """
        SELECT
            students.id AS student_id,
            students.branch_id AS branch_id,
            students.name AS student_name,
            students.enrollment AS enrollment,
            students.email AS email,
            SUM(CASE WHEN attendance.status = 'Present' THEN 1 ELSE 0 END) AS present_count,
            SUM(CASE WHEN attendance.status = 'Absent' THEN 1 ELSE 0 END) AS absent_count,
            COUNT(attendance.id) AS total_count
        FROM students
        LEFT JOIN attendance ON attendance.student_id = students.id
        GROUP BY students.id, students.branch_id, students.name, students.enrollment, students.email
        ORDER BY students.name
        """
    ).fetchall():
        branch_id = row_get(row, "branch_id")
        total = row_get(row, "total_count", 0) or 0
        present = row_get(row, "present_count", 0) or 0
        absent = row_get(row, "absent_count", 0) or 0
        pct = round((present / total) * 100, 1) if total else 0
        students_by_branch.setdefault(branch_id, []).append(
            {
                "id": row_get(row, "student_id"),
                "name": row_get(row, "student_name"),
                "enrollment": row_get(row, "enrollment"),
                "email": row_get(row, "email"),
                "present": present,
                "absent": absent,
                "total": total,
                "pct": pct,
            }
        )

    departments_data = []
    for dept in departments:
        dept_id = row_get(dept, "id")
        attendance_total = attendance_counts.get(dept_id, 0)
        present = present_counts.get(dept_id, 0)
        absent = absent_counts.get(dept_id, 0)
        attendance_pct = round((present / attendance_total) * 100, 1) if attendance_total else 0
        departments_data.append(
            {
                "id": dept_id,
                "name": row_get(dept, "name"),
                "location": row_get(dept, "location"),
                "student_count": student_counts.get(dept_id, 0),
                "subject_count": subject_counts.get(dept_id, 0),
                "attendance_count": attendance_total,
                "present_count": present,
                "absent_count": absent,
                "attendance_pct": attendance_pct,
                "subjects": subjects_by_branch.get(dept_id, []),
                "students": students_by_branch.get(dept_id, []),
            }
        )

    db.close()

    total_students_value = row_get(total_students, "count", 0) or 0
    total_subjects_value = row_get(total_subjects, "count", 0) or 0
    total_attendance_value = row_get(total_attendance, "count", 0) or 0
    render_env = bool(os.environ.get("RENDER") or os.environ.get("RENDER_INTERNAL_HOSTNAME"))
    persistence_warning = render_env and not str(app.config.get("DATABASE", "")).startswith("postgres")

    return render_template(
        "department_dashboard.html",
        departments=departments_data,
        total_students=total_students_value,
        total_subjects=total_subjects_value,
        total_attendance=total_attendance_value,
        overall_percentage=overall_percentage,
        persistence_warning=persistence_warning,
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


@app.route("/test-email")
@app.route("/test_email")
@login_required
def test_email():
    if session.get("role") != "admin":
        return redirect(url_for("dashboard"))

    if not is_mail_configured():
        flash("Email is not configured. Please set MAIL_USERNAME and MAIL_PASSWORD.", "error")
        return redirect(url_for("settings"))

    recipient = (app.config.get("REPORT_ADMIN_EMAIL") or "").strip()
    if not recipient or not is_valid_email(recipient):
        flash("Admin email is invalid. Update REPORT_ADMIN_EMAIL.", "error")
        return redirect(url_for("settings"))

    body = (
        "Test email from Attendance Management System.\n\n"
        f"Sent to: {recipient}\n"
        f"Time: {date.today().isoformat()}\n"
    )
    email_sent = safe_send_email(
        subject="Test Email: Attendance System",
        recipient=recipient,
        body=body,
    )
    if email_sent:
        flash(f"Test email sent to {recipient}.", "success")
    else:
        flash("Failed to send test email. Check mail settings.", "error")

    return redirect(url_for("settings"))


@app.route("/admin/import_data", methods=["POST"])
@login_required
def admin_import_data():
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid JSON payload"}), 400

    db = get_db()
    placeholder = get_placeholder()
    is_postgres = str(app.config.get("DATABASE", "")).startswith("postgres")

    def insert_ignore(table, columns, values):
        cols = ", ".join(columns)
        placeholders = ", ".join([placeholder] * len(columns))
        if is_postgres:
            query = f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
        else:
            query = f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})"
        db.execute(query, tuple(values))

    def sync_sequence(table, id_col="id"):
        if not is_postgres:
            return
        try:
            db.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', '{id_col}'), "
                f"COALESCE(MAX({id_col}), 0), COALESCE(MAX({id_col}), 0) > 0) FROM {table}"
            )
        except Exception as e:
            print(f"[import_data] sequence sync failed for {table}: {repr(e)}")

    counts = {"branches": 0, "subjects": 0, "students": 0, "attendance": 0, "users": 0}
    try:
        for row in payload.get("branches", []) or []:
            insert_ignore(
                "branches",
                ["id", "name", "location"],
                [row.get("id"), row.get("name"), row.get("location")],
            )
            counts["branches"] += 1

        for row in payload.get("subjects", []) or []:
            insert_ignore(
                "subjects",
                ["id", "name", "branch_id"],
                [row.get("id"), row.get("name"), row.get("branch_id")],
            )
            counts["subjects"] += 1

        students_payload = payload.get("students", []) or []
        for row in students_payload:
            insert_ignore(
                "students",
                ["id", "name", "enrollment", "branch_id", "email"],
                [
                    row.get("id"),
                    row.get("name"),
                    row.get("enrollment"),
                    row.get("branch_id"),
                    row.get("email"),
                ],
            )
            counts["students"] += 1

        for row in payload.get("attendance", []) or []:
            insert_ignore(
                "attendance",
                ["id", "student_id", "branch_id", "subject_id", "date", "status", "note"],
                [
                    row.get("id"),
                    row.get("student_id"),
                    row.get("branch_id"),
                    row.get("subject_id"),
                    row.get("date"),
                    row.get("status"),
                    row.get("note"),
                ],
            )
            counts["attendance"] += 1

        for row in students_payload:
            enrollment = (row.get("enrollment") or "").strip()
            student_id = row.get("id")
            if not enrollment or not student_id:
                continue
            default_password = enrollment[-4:] if len(enrollment) >= 4 else enrollment
            insert_ignore(
                "users",
                ["username", "password", "role", "student_id"],
                [enrollment, generate_password_hash(default_password), "student", student_id],
            )
            counts["users"] += 1

        sync_sequence("branches")
        sync_sequence("subjects")
        sync_sequence("students")
        sync_sequence("attendance")
        sync_sequence("users")

        db.commit()
        return jsonify({"message": "Import completed", "counts": counts})
    except Exception as e:
        db.rollback()
        print(f"[import_data] ERROR: {repr(e)}")
        return jsonify({"error": "Import failed. Check server logs."}), 500
    finally:
        try:
            db.close()
        except Exception:
            pass

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


@app.route("/upload_students", methods=["GET", "POST"])
@login_required
def upload_students():
    if session.get("role") != "admin":
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose an Excel (.xlsx) file to upload.", "error")
            return redirect(url_for("upload_students"))

        filename = secure_filename(file.filename)
        if not filename.lower().endswith(".xlsx"):
            flash("Only .xlsx files are supported.", "error")
            return redirect(url_for("upload_students"))

        try:
            import pandas as pd
        except Exception:
            flash("pandas is not installed. Please add pandas and openpyxl to requirements.", "error")
            return redirect(url_for("upload_students"))

        try:
            import pandas as pd
            # Read entire file first to find the header row
            all_data = pd.read_excel(file, header=None)
            
            # Find the header row by searching for keywords
            header_idx = 0
            found_header = False
            for i, row in all_data.iterrows():
                row_str = [str(cell).lower() for cell in row]
                if any(k in " ".join(row_str) for k in ["name", "enrollment", "h.t.no", "mail", "branch", "section"]):
                    header_idx = i
                    found_header = True
                    break
            
            # Re-read or just slice the dataframe
            df = pd.read_excel(file, skiprows=header_idx)
        except Exception as e:
            print(f"[upload_students] Failed to read Excel: {repr(e)}")
            flash("Failed to read the Excel file. Please check the format.", "error")
            return redirect(url_for("upload_students"))

        # Map aliases to standard column names
        column_mapping = {
            'name': 'name',
            'student name': 'name',
            'name of the students': 'name',
            'student': 'name',
            'enrollment': 'enrollment',
            'enrollment no': 'enrollment',
            'h.t.no': 'enrollment',
            'hall ticket no': 'enrollment',
            'ten digits h.t.no': 'enrollment',
            'roll no': 'enrollment',
            'email': 'email',
            'mail id': 'email',
            'email id': 'email',
            'branch_id': 'branch_id',
            'branch id': 'branch_id',
            'branch': 'branch_id',
            'section': 'branch_id'
        }

        # Normalize existing columns and rename based on mapping
        current_cols = [str(c).strip().lower() for c in df.columns]
        new_cols = []
        for col in current_cols:
            found = False
            for alias, target in column_mapping.items():
                if alias in col: # partial match for robustness
                    new_cols.append(target)
                    found = True
                    break
            if not found:
                new_cols.append(col)
        
        df.columns = new_cols
        required = {"name", "enrollment", "email", "branch_id"}
        missing = required - set(df.columns)
        
        if missing:
            flash(f"Missing columns: {', '.join(sorted(missing))}. We searched for keywords like 'Name', 'Enrollment', 'Mail', 'Branch', and 'Section'.", "error")
            return redirect(url_for("upload_students"))

        db = get_db()
        placeholder = get_placeholder()
        is_postgres = str(app.config.get("DATABASE", "")).startswith("postgres")
        inserted = 0
        skipped = 0
        errors = 0

        try:
            # Pre-fetch branches for name matching
            branches_map = {}
            for b in db.execute("SELECT id, name FROM branches").fetchall():
                # Support both name and ID lookups
                b_name = row_get(b, "name")
                b_id = row_get(b, "id")
                if b_name is not None:
                    branches_map[str(b_name).lower()] = b_id
                if b_id is not None:
                    branches_map[str(b_id)] = b_id

            for _, row in df.iterrows():
                name = str(row.get("name", "")).strip()
                enrollment = str(row.get("enrollment", "")).strip()
                email = str(row.get("email", "")).strip()
                branch_id_raw = str(row.get("branch_id", "")).strip()

                if not name or not enrollment:
                    errors += 1
                    continue

                if not branch_id_raw or branch_id_raw.lower() == "nan":
                    errors += 1
                    continue

                # Try exact match first, then partial match for branch name
                branch_id = None
                branch_id_raw_lower = branch_id_raw.lower()
                
                if branch_id_raw_lower in branches_map:
                    branch_id = branches_map[branch_id_raw_lower]
                else:
                    # Partial match: if "CSM" is in "I CSM-B"
                    for b_name, b_id in branches_map.items():
                        if b_name and b_name in branch_id_raw_lower:
                            branch_id = b_id
                            break
                
                if branch_id is None:
                    print(f"[upload_students] Branch not found for: {branch_id_raw}")
                    errors += 1
                    continue

                email_value = email or None

                if is_postgres:
                    student_row = db.execute(
                        f"""
                        INSERT INTO students (name, enrollment, email, branch_id)
                        VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                        ON CONFLICT (enrollment) DO NOTHING
                        RETURNING id
                        """,
                        (name, enrollment, email_value, branch_id),
                    ).fetchone()
                    student_id = row_get(student_row, "id")
                    if not student_id:
                        skipped += 1
                        continue
                else:
                    cur = db.execute(
                        f"INSERT OR IGNORE INTO students (name, enrollment, email, branch_id) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
                        (name, enrollment, email_value, branch_id),
                    )
                    if getattr(cur, "rowcount", 0) == 0:
                        skipped += 1
                        continue
                    student_id = cur.lastrowid

                default_password = enrollment[-4:] if len(enrollment) >= 4 else enrollment
                if is_postgres:
                    db.execute(
                        f"""
                        INSERT INTO users (username, password, role, student_id)
                        VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                        ON CONFLICT (username) DO NOTHING
                        """,
                        (enrollment, generate_password_hash(default_password), "student", student_id),
                    )
                else:
                    db.execute(
                        f"INSERT OR IGNORE INTO users (username, password, role, student_id) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
                        (enrollment, generate_password_hash(default_password), "student", student_id),
                    )

                inserted += 1

            db.commit()
            flash(
                f"Upload complete: {inserted} added, {skipped} skipped, {errors} errors.",
                "success",
            )
        except Exception as e:
            db.rollback()
            print(f"[upload_students] ERROR: {repr(e)}")
            flash("Upload failed due to a server error. Please try again.", "error")
        finally:
            try:
                db.close()
            except Exception:
                pass

        return redirect(url_for("upload_students"))

    return render_template("upload_students.html")


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

    search = request.args.get("search", "").strip()
    branch_filter = request.args.get("branch_id", "").strip()

    query = "SELECT students.*, branches.name AS branch_name FROM students JOIN branches ON students.branch_id = branches.id"
    clauses = []
    params = []

    if search:
        like_op = "ILIKE" if str(app.config.get("DATABASE", "")).startswith("postgres") else "LIKE"
        clauses.append(f"(students.name {like_op} {placeholder} OR students.enrollment {like_op} {placeholder})")
        params.extend([f"%{search}%", f"%{search}%"])

    if branch_filter:
        clauses.append(f"students.branch_id = {placeholder}")
        params.append(branch_filter)

    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    query += " ORDER BY students.name"
    students = db.execute(query, params).fetchall()

    db.close()
    return render_template("students.html", students=students, branches=branches, search=search, selected_branch_id=branch_filter)


@app.route("/student_login", methods=["GET", "POST"])
def student_login():
    next_url = request.args.get("next") or request.form.get("next")

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not username or not password:
            flash("Please enter username and password.", "error")
            return render_template("student_login.html", next=next_url)

        db = None
        try:
            db = get_db()
            placeholder = get_placeholder()
            user = db.execute(
                f"SELECT id, username, password, role, student_id FROM users WHERE username = {placeholder}",
                (username,),
            ).fetchone()

            if user and row_get(user, "role") == "student" and check_password_hash(row_get(user, "password"), password):
                session.clear()
                session["user_id"] = row_get(user, "id")
                session["username"] = row_get(user, "username")
                session["role"] = row_get(user, "role")
                session["student_id"] = row_get(user, "student_id")
                if next_url:
                    return redirect(next_url)
                return redirect(url_for("student_dashboard"))

            flash("Invalid student login credentials.", "error")

        except Exception as e:
            print(f"[student_login] ERROR: {repr(e)}")
            print(traceback.format_exc())
            flash("Student login is temporarily unavailable (database error).", "error")
        finally:
            try:
                if db is not None:
                    db.close()
            except Exception:
                pass

    return render_template("student_login.html", next=next_url)


@app.route("/teacher_login", methods=["GET", "POST"])
def teacher_login():
    if request.method == "POST":
        username = (request.form.get("username", "") or "").strip()
        password = (request.form.get("password", "") or "").strip()

        if not username or not password:
            flash("Please enter username and password.", "error")
            return render_template("teacher_login.html")

        db = None
        try:
            db = get_db()
            placeholder = get_placeholder()
            user = db.execute(
                f"SELECT id, username, password, role FROM users WHERE username = {placeholder}",
                (username,),
            ).fetchone()

            if user and row_get(user, "role") == "teacher" and check_password_hash(row_get(user, "password"), password):
                session.clear()
                session["user_id"] = row_get(user, "id")
                session["username"] = row_get(user, "username")
                session["role"] = row_get(user, "role")
                return redirect(url_for("dashboard"))

            flash("Invalid teacher login credentials.", "error")

        except Exception as e:
            print(f"[teacher_login] ERROR: {repr(e)}")
            print(traceback.format_exc())
            flash("Teacher login is temporarily unavailable (database error).", "error")
        finally:
            try:
                if db is not None:
                    db.close()
            except Exception:
                pass

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
    absent = total - present
    percentage = round((present / total) * 100, 1) if total > 0 else 0

    # Subject-wise attendance for the chart
    subject_stats = []
    for sub in subjects:
        sub_id = sub["id"]
        sub_records = [r for r in attendance_records if r["subject_id"] == sub_id]
        sub_total = len(sub_records)
        sub_present = len([r for r in sub_records if r["status"] == "Present"])
        sub_pct = round((sub_present / sub_total) * 100, 1) if sub_total > 0 else 0
        subject_stats.append({
            "name": sub["name"],
            "percentage": sub_pct
        })

    subject_chart_labels = [s["name"] for s in subject_stats]
    subject_chart_percentages = [s["percentage"] for s in subject_stats]

    student_qr_data_uri = None
    try:
        # Generate a small QR for quick student identification.
        # Payload is intentionally simple so it remains stable.
        import base64
        from io import BytesIO

        import qrcode

        enrollment = str(student.get("enrollment") or "")
        payload = f"ENROLLMENT:{enrollment}" if enrollment else f"STUDENT_ID:{student_id}"
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=6,
            border=2,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        student_qr_data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        # QR is a convenience feature; do not fail the dashboard if it can't be generated.
        print(f"[student_dashboard] QR generation skipped: {repr(e)}")

    db.close()
    return render_template(
        "student_dashboard.html",
        student=student,
        attendance_records=attendance_records,
        total_classes=total,
        present_count=present,
        absent_count=absent,
        percentage=percentage,
        subjects=subjects,
        subject_chart_labels=subject_chart_labels,
        subject_chart_percentages=subject_chart_percentages,
        selected_subject_id=selected_subject_id,
        student_qr_data_uri=student_qr_data_uri,
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


def get_report_filters():
    return {
        "branch_id": request.args.get("branch_id") or request.form.get("branch_id"),
        "subject_id": request.args.get("subject_id") or request.form.get("subject_id"),
        "student_id": request.args.get("student_id") or request.form.get("student_id"),
        "from_date": request.args.get("from_date") or request.form.get("from_date"),
        "to_date": request.args.get("to_date") or request.form.get("to_date"),
        "search": request.args.get("search") or request.form.get("search"),
    }


def fetch_report_records(db, filters):
    placeholder = get_placeholder()
    query = (
        "SELECT attendance.*, students.name AS student_name, students.enrollment, "
        "branches.name AS branch_name, subjects.name AS subject_name "
        "FROM attendance "
        "JOIN students ON attendance.student_id = students.id "
        "JOIN branches ON attendance.branch_id = branches.id "
        "JOIN subjects ON attendance.subject_id = subjects.id"
    )
    clauses = []
    params = []

    if filters.get("branch_id"):
        clauses.append(f"attendance.branch_id = {placeholder}")
        params.append(filters["branch_id"])
    if filters.get("subject_id"):
        clauses.append(f"attendance.subject_id = {placeholder}")
        params.append(filters["subject_id"])
    if filters.get("student_id"):
        clauses.append(f"attendance.student_id = {placeholder}")
        params.append(filters["student_id"])
    if filters.get("from_date"):
        clauses.append(f"attendance.date >= {placeholder}")
        params.append(filters["from_date"])
    if filters.get("to_date"):
        clauses.append(f"attendance.date <= {placeholder}")
        params.append(filters["to_date"])
    if filters.get("search"):
        s = f"%{filters['search']}%"
        like_op = "ILIKE" if str(app.config.get("DATABASE", "")).startswith("postgres") else "LIKE"
        clauses.append(f"(students.name {like_op} {placeholder} OR students.enrollment {like_op} {placeholder})")
        params.extend([s, s])

    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    query += " ORDER BY attendance.date DESC, students.name"
    return db.execute(query, params).fetchall()


def build_report_stats(records):
    stats = {}
    if not records:
        return stats

    student_stats = {}
    subject_stats = {}
    total_records = len(records)

    for record in records:
        student_id = row_get(record, "student_id")
        subject_id = row_get(record, "subject_id")
        status = row_get(record, "status")

        if student_id not in student_stats:
            student_stats[student_id] = {
                "total": 0,
                "present": 0,
                "name": row_get(record, "student_name"),
                "enrollment": row_get(record, "enrollment"),
            }
        student_stats[student_id]["total"] += 1
        if status == "Present":
            student_stats[student_id]["present"] += 1

        if subject_id not in subject_stats:
            subject_stats[subject_id] = {
                "total": 0,
                "present": 0,
                "name": row_get(record, "subject_name"),
            }
        subject_stats[subject_id]["total"] += 1
        if status == "Present":
            subject_stats[subject_id]["present"] += 1

    for student_id, data in student_stats.items():
        data["percentage"] = round((data["present"] / data["total"]) * 100, 1) if data["total"] > 0 else 0

    for subject_id, data in subject_stats.items():
        data["percentage"] = round((data["present"] / data["total"]) * 100, 1) if data["total"] > 0 else 0

    stats = {
        "student_stats": list(student_stats.values()),
        "subject_stats": list(subject_stats.values()),
        "total_records": total_records,
        "overall_present": sum(s["present"] for s in student_stats.values()),
        "overall_total": sum(s["total"] for s in student_stats.values()),
    }
    if stats["overall_total"] > 0:
        stats["overall_percentage"] = round((stats["overall_present"] / stats["overall_total"]) * 100, 1)
    else:
        stats["overall_percentage"] = 0

    return stats


def build_report_excel(records):
    rows = []
    for record in records:
        rows.append(
            {
                "Date": row_get(record, "date"),
                "Student": row_get(record, "student_name"),
                "Enrollment": row_get(record, "enrollment"),
                "Branch": row_get(record, "branch_name"),
                "Subject": row_get(record, "subject_name"),
                "Status": row_get(record, "status"),
                "Note": row_get(record, "note"),
            }
        )

    import pandas as pd

    output = BytesIO()
    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(columns=["Date", "Student", "Enrollment", "Branch", "Subject", "Status", "Note"])
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Attendance")
    output.seek(0)

    filename = f"attendance_report_{date.today().isoformat()}.xlsx"
    return output.getvalue(), filename


@app.route("/download_attendance")
@login_required
def download_attendance():
    """Download attendance as an Excel file.

    Optional filters (query params):
    - branch_id
    - subject_id
    - from_date / to_date (or start_date / end_date)

    For safety, if a student is logged in, the export is limited to that student.
    """

    filters = {
        "branch_id": (request.args.get("branch_id") or "").strip() or None,
        "subject_id": (request.args.get("subject_id") or "").strip() or None,
        "from_date": (
            (request.args.get("from_date") or request.args.get("start_date") or "").strip() or None
        ),
        "to_date": (
            (request.args.get("to_date") or request.args.get("end_date") or "").strip() or None
        ),
    }

    if session.get("role") == "student":
        filters["student_id"] = session.get("student_id")

    db = get_db()
    try:
        records = fetch_report_records(db, filters)

        rows = []
        for record in records:
            rows.append(
                {
                    "Date": row_get(record, "date"),
                    "Student": row_get(record, "student_name"),
                    "Subject": row_get(record, "subject_name"),
                    "Status": row_get(record, "status"),
                }
            )

        import pandas as pd

        output = BytesIO()
        df = pd.DataFrame(rows)
        if df.empty:
            df = pd.DataFrame(columns=["Date", "Student", "Subject", "Status"])

        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Attendance")
        output.seek(0)

        filename = f"attendance_report_{date.today().isoformat()}.xlsx"
        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


@app.route("/reports", methods=["GET", "POST"])
@login_required
def attendance_report():
    db = get_db()
    branches = db.execute(f"SELECT * FROM branches ORDER BY name").fetchall()
    subjects = []
    records = []
    filters = get_report_filters()
    placeholder = get_placeholder()

    if filters["branch_id"]:
        subjects = db.execute(
            f"SELECT * FROM subjects WHERE branch_id = {placeholder} ORDER BY name", (filters["branch_id"],)
        ).fetchall()

    records = fetch_report_records(db, filters)
    students = []
    if filters["branch_id"]:
        students = db.execute(
            f"SELECT * FROM students WHERE branch_id = {placeholder} ORDER BY name", (filters["branch_id"],)
        ).fetchall()

    stats = build_report_stats(records)

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


@app.route("/reports/export")
@login_required
def export_excel():
    db = get_db()
    try:
        filters = get_report_filters()
        records = fetch_report_records(db, filters)
        content, filename = build_report_excel(records)
        return send_file(
            BytesIO(content),
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


@app.route("/reports/email", methods=["POST"])
@login_required
def report_email():
    filters = get_report_filters()
    redirect_params = {k: v for k, v in filters.items() if v}

    if not is_mail_configured():
        flash("Email is not configured. Please set MAIL_USERNAME and MAIL_PASSWORD.", "error")
        return redirect(url_for("attendance_report", **redirect_params))

    recipient = (app.config.get("REPORT_ADMIN_EMAIL") or "").strip()
    if not recipient:
        flash("Email recipient is not configured.", "error")
        return redirect(url_for("attendance_report", **redirect_params))

    if not is_valid_email(recipient):
        flash("Email recipient is invalid.", "error")
        return redirect(url_for("attendance_report", **redirect_params))

    db = get_db()
    try:
        placeholder = get_placeholder()
        branch_name = "All branches"
        subject_name = "All subjects"
        student_name = "All students"

        if filters.get("branch_id"):
            branch_row = db.execute(
                f"SELECT name FROM branches WHERE id = {placeholder}",
                (filters["branch_id"],),
            ).fetchone()
            branch_name = row_get(branch_row, "name") or branch_name

        if filters.get("subject_id"):
            subject_row = db.execute(
                f"SELECT name FROM subjects WHERE id = {placeholder}",
                (filters["subject_id"],),
            ).fetchone()
            subject_name = row_get(subject_row, "name") or subject_name

        if filters.get("student_id"):
            student_row = db.execute(
                f"SELECT name FROM students WHERE id = {placeholder}",
                (filters["student_id"],),
            ).fetchone()
            student_name = row_get(student_row, "name") or student_name

        records = fetch_report_records(db, filters)
        stats = build_report_stats(records)
        content, filename = build_report_excel(records)

        body_lines = [
            "Attendance Report",
            "",
            f"Branch: {branch_name}",
            f"Subject: {subject_name}",
            f"Student: {student_name}",
        ]
        if filters.get("from_date") or filters.get("to_date"):
            body_lines.append(
                f"Date range: {filters.get('from_date') or 'Any'} to {filters.get('to_date') or 'Any'}"
            )
        body_lines.extend(
            [
                "",
                f"Total records: {stats.get('total_records', 0)}",
                f"Overall attendance: {stats.get('overall_percentage', 0)}%",
            ]
        )

        email_sent = safe_send_email(
            subject="Attendance Report",
            recipient=recipient,
            body="\n".join(body_lines),
            attachments=[
                {
                    "filename": filename,
                    "content": content,
                    "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                }
            ],
        )

        if email_sent:
            flash(f"Report emailed successfully to {recipient}.", "success")
        else:
            flash("Failed to send the report email.", "error")
    finally:
        try:
            db.close()
        except Exception:
            pass

    return redirect(url_for("attendance_report", **redirect_params))


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
        identifier = (request.form.get("email", "") or "").strip()

        # Debug logs (show up in Render logs)
        print("[forgot_password] POST received")
        print(f"[forgot_password] identifier_present={bool(identifier)} identifier_type={'email' if '@' in identifier else 'enrollment'}")

        if not identifier:
            flash("Please enter your registered email address or enrollment number.", "error")
            return render_template("forgot_password.html")

        if not is_mail_configured():
            print("[forgot_password] mail not configured")
            flash("Email service is not configured. Contact the administrator.", "error")
            return render_template("forgot_password.html")

        db = None
        try:
            db = get_db()
            placeholder = get_placeholder()
            is_postgres = str(app.config.get("DATABASE", "")).startswith("postgres")
            print(f"[forgot_password] db={'postgres' if is_postgres else 'sqlite'}")

            # 1) Find student by email OR enrollment
            student = None
            if "@" in identifier:
                student = db.execute(
                    f"SELECT id, email, enrollment FROM students WHERE LOWER(email) = LOWER({placeholder})",
                    (identifier,),
                ).fetchone()
            else:
                student = db.execute(
                    f"SELECT id, email, enrollment FROM students WHERE LOWER(enrollment) = LOWER({placeholder})",
                    (identifier,),
                ).fetchone()

            if student:
                print(f"[forgot_password] student_found=True student_id={row_get(student, 'id')}")
            else:
                print("[forgot_password] student_found=False")

            # 2) Find matching student user
            user = None
            if student:
                user = db.execute(
                    f"SELECT id, username, student_id FROM users WHERE role = {placeholder} AND student_id = {placeholder}",
                    ("student", row_get(student, "id")),
                ).fetchone()

            # Fallback A: if users.student_id is missing/mismatched, try username matching
            if not user:
                user = db.execute(
                    f"SELECT id, username, student_id FROM users WHERE role = {placeholder} AND LOWER(username) = LOWER({placeholder})",
                    ("student", identifier),
                ).fetchone()
                if user:
                    print("[forgot_password] user_found_by_username=True")

            if user:
                print(f"[forgot_password] user_found=True user_id={row_get(user, 'id')} username={row_get(user, 'username')}")
            else:
                print("[forgot_password] user_found=False")

            # 3) Decide email to send to
            student_email = (row_get(student, "email") or "").strip()
            if not student_email and user and row_get(user, "student_id"):
                # Fallback B: if we found user first, load student email from student_id
                linked_student = db.execute(
                    f"SELECT email FROM students WHERE id = {placeholder}",
                    (row_get(user, "student_id"),),
                ).fetchone()
                student_email = (row_get(linked_student, "email") or "").strip()
                if student_email:
                    print("[forgot_password] student_email_resolved_via_user_student_id=True")

            if student and not student_email:
                print("[forgot_password] student found but email missing")
                flash(
                    "Your account does not have an email address on file. Contact the administrator to reset your password.",
                    "error",
                )
                return render_template("forgot_password.html")

            # 4) Generate token + send email (only if we have everything)
            email_sent = False
            if user and student_email:
                token = generate_reset_token(row_get(user, "id"))
                reset_link = url_for("reset_password", token=token, _external=True)
                print(f"[forgot_password] reset_link_generated host={request.host}")

                body = (
                    "Hello,\n\n"
                    "We received a request to reset your password. Use the link below to set a new password:\n\n"
                    f"{reset_link}\n\n"
                    "If you did not request a reset, you can ignore this email.\n\n"
                    "Regards,\n"
                    "Attendance Management Team"
                )
                email_sent = safe_send_email(
                    subject="Reset your password",
                    recipient=student_email,
                    body=body,
                )
                print(f"[forgot_password] email_attempted=True email_sent={email_sent}")
            else:
                print("[forgot_password] email_attempted=False (no matching user or no email)")

            # Always show generic message to avoid account enumeration
            flash("If this account exists and has an email, a reset link has been sent.", "success")
            return render_template("forgot_password.html")

        except Exception as e:
            # Never crash this route in production
            print(f"[forgot_password] ERROR: {repr(e)}")
            flash("Something went wrong while processing your request. Please try again.", "error")
            return render_template("forgot_password.html")
        finally:
            try:
                if db is not None:
                    db.close()
            except Exception:
                pass

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
@app.route("/reset_password/<token>", methods=["GET", "POST"])
def reset_password(token):
    print("[reset_password] request")
    try:
        user_id, error = verify_reset_token(token)
        if error:
            print(f"[reset_password] token_error={error}")
            flash("Reset link is invalid or expired. Please request a new one.", "error")
            return redirect(url_for("forgot_password"))

        db = get_db()
        placeholder = get_placeholder()
        try:
            user = db.execute(
                f"SELECT id, role FROM users WHERE id = {placeholder}",
                (user_id,),
            ).fetchone()

            if not user or row_get(user, "role") != "student":
                print("[reset_password] user_not_found_or_not_student")
                flash("Reset link is invalid or expired. Please request a new one.", "error")
                return redirect(url_for("forgot_password"))

            if request.method == "POST":
                password = (request.form.get("password", "") or "").strip()
                confirm_password = (request.form.get("confirm_password", "") or "").strip()

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
                    print(f"[reset_password] password_updated user_id={user_id}")
                    flash("Password updated successfully. Please log in.", "success")
                    return redirect(url_for("student_login"))

            return render_template("reset_password.html")
        finally:
            try:
                db.close()
            except Exception:
                pass

    except Exception as e:
        print(f"[reset_password] ERROR: {repr(e)}")
        flash("Reset link is invalid or expired. Please request a new one.", "error")
        return redirect(url_for("forgot_password"))


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


@app.route('/admin/check-db')
@login_required
def admin_check_db():
    # Only admins may run this check
    if session.get('role') != 'admin':
        abort(403)

    db = get_db()
    try:
        db_url = str(app.config.get('DATABASE', ''))
        is_postgres = db_url.startswith('postgres')
        info = {
            'ok': True,
            'db': 'postgres' if is_postgres else 'sqlite',
            'database': db_url,
        }
        tables = ['branches', 'students', 'subjects', 'attendance', 'users', 'settings']
        counts = {}
        for t in tables:
            try:
                counts[t] = int(db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            except Exception as e:
                counts[t] = f"error: {repr(e)}"
        info['counts'] = counts

        # Show a small hint if DB is empty
        if all(isinstance(counts.get(t), int) and counts[t] == 0 for t in tables if t in counts):
            info['hint'] = (
                "All tables are empty. This usually means you are connected to a new database "
                "(for example, you switched from SQLite to PostgreSQL or a new Render Postgres was created)."
            )

        return jsonify(info)
    finally:
        try:
            db.close()
        except Exception:
            pass
with app.app_context():
    try:
        print(f"Database path: {app.config['DATABASE']}")
        db_str = str(app.config.get('DATABASE', ''))
        if not db_str.startswith('postgres'):
            print(f"Database file exists: {os.path.exists(db_str)}")
            if os.path.exists(db_str):
                db_size = os.path.getsize(db_str)
                print(f"Database file size: {db_size} bytes")

        # Best-effort schema initialization at startup (won't crash the app).
        init_db()
        print("Database initialized successfully")
    except Exception as e:
        print(f"Database initialization failed: {repr(e)}")
        print(traceback.format_exc())
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
