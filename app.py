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
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)

# Initialize SocketIO after app creation
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
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
    db = None
    if db_url.startswith("postgres"):
        try:
            import psycopg2
            from psycopg2.extras import DictCursor
        except Exception as e:
            print("[DB] psycopg2 import failed.")
            raise

        def _ensure_sslmode(url: str) -> str:
            if "sslmode=" in url: return url
            is_render = bool(os.environ.get("RENDER") or os.environ.get("RENDER_INTERNAL_HOSTNAME"))
            if not is_render: return url
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}sslmode=require"

        class _PostgresDB:
            def __init__(self, conn):
                self._conn = conn
            def execute(self, query, params=()):
                cur = self._conn.cursor(cursor_factory=DictCursor)
                cur.execute(query, params)
                return cur
            def commit(self): return self._conn.commit()
            def rollback(self): return self._conn.rollback()
            def close(self): return self._conn.close()

        try:
            conn = psycopg2.connect(_ensure_sslmode(db_url), connect_timeout=10)
            db = _PostgresDB(conn)
        except Exception as e:
            print(f"[DB] PostgreSQL connection error: {repr(e)}")
            raise
    else:
        import sqlite3
        conn = sqlite3.connect(db_url, timeout=20)
        conn.row_factory = sqlite3.Row
        db = conn

    if db:
        try:
            ensure_db_initialized(db)
        except:
            pass
    return db


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


def send_email(subject, recipient, body, attachments=None, html_body=None):
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
        if html_body:
            msg.add_alternative(html_body, subtype="html")

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


def safe_send_email(subject: str, recipient: str, body: str, attachments=None, html_body=None) -> bool:
    """Wrapper around send_email() that guarantees no exception escapes."""
    try:
        return bool(
            send_email(
                subject=subject,
                recipient=recipient,
                body=body,
                attachments=attachments,
                html_body=html_body,
            )
        )
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
            html_body = (
                f"<div style='font-family:Arial,sans-serif;line-height:1.6;color:#1f2937'>"
                f"<h2 style='margin-bottom:8px;color:#ef476f'>Low Attendance Alert</h2>"
                f"<p>Hello <strong>{row['student_name']}</strong>,</p>"
                f"<p>Your current attendance is <strong>{row['percentage']}%</strong>, which is below the required threshold of <strong>{threshold}%</strong>.</p>"
                "<p>Please attend classes regularly and check your dashboard for details.</p>"
                "<p style='margin-top:16px'>Best regards,<br>Attendance Management Team</p>"
                "</div>"
            )
            if send_email(
                subject=f"Low Attendance Alert: {row['percentage']}%",
                recipient=row["email"],
                body=body,
                html_body=html_body,
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
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("You must be logged in to view this page.", "warning")
            return redirect(url_for("login"))
        if session.get("role") == "student":
            flash("You do not have permission to access this page.", "danger")
            return redirect(url_for("student_dashboard"))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in as admin to continue.", "warning")
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            flash("Admin access required.", "danger")
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated_function
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
    """Main admin dashboard with stats and charts."""
    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()

        def _safe_scalar(query, params=(), default=0):
            try:
                row = db.execute(query, params).fetchone()
                if row is None:
                    return default
                try:
                    return row[0]
                except Exception:
                    return default
            except Exception as qe:
                print(f"[dashboard] scalar query failed: {repr(qe)} | query={query}")
                return default

        def _safe_fetchall(query, params=()):
            try:
                return db.execute(query, params).fetchall()
            except Exception as qe:
                print(f"[dashboard] fetchall query failed: {repr(qe)} | query={query}")
                return []

        branch_count = int(_safe_scalar("SELECT COUNT(*) FROM branches", default=0) or 0)
        student_count = int(_safe_scalar("SELECT COUNT(*) FROM students", default=0) or 0)
        subject_count = int(_safe_scalar("SELECT COUNT(*) FROM subjects", default=0) or 0)
        attendance_count = int(_safe_scalar("SELECT COUNT(*) FROM attendance", default=0) or 0)

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
            pass

        overall_percentage = 0
        if total_classes > 0:
            overall_percentage = round((present_count / total_classes) * 100, 1)

        subject_rows = _safe_fetchall(
            """
            SELECT
                subjects.name AS subject_name,
                COUNT(attendance.id) AS total_count,
                SUM(CASE WHEN attendance.status = 'Present' THEN 1 ELSE 0 END) AS present_count
            FROM subjects
            LEFT JOIN attendance ON subjects.id = attendance.subject_id
            GROUP BY subjects.id, subjects.name
            ORDER BY subjects.name
            """
        )

        subject_chart_labels = []
        subject_chart_percentages = []
        for row in subject_rows:
            total = int(row_get(row, "total_count", 0) or 0)
            present = int(row_get(row, "present_count", 0) or 0)
            pct = round((present / total) * 100, 1) if total > 0 else 0
            subject_chart_labels.append(row_get(row, "subject_name", "") or "")
            subject_chart_percentages.append(pct)

        trend_rows = _safe_fetchall(
            """
            SELECT
                date,
                COUNT(*) AS total_count,
                SUM(CASE WHEN status = 'Present' THEN 1 ELSE 0 END) AS present_count
            FROM attendance
            GROUP BY date
            ORDER BY date DESC
            LIMIT 14
            """
        )
        trend_labels = []
        trend_percentages = []
        for row in reversed(trend_rows):
            total = int(row_get(row, "total_count", 0) or 0)
            present = int(row_get(row, "present_count", 0) or 0)
            pct = round((present / total) * 100, 1) if total > 0 else 0
            trend_labels.append(str(row_get(row, "date", "") or ""))
            trend_percentages.append(pct)

        branch_data = _safe_fetchall(
            """
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
            """
        )
        if not branch_data:
            # Backward-compatible fallback for old schemas that don't have branches.location.
            branch_data = _safe_fetchall(
                """
                SELECT
                    branches.name AS branch_name,
                    '' AS location,
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
                GROUP BY branches.id, branches.name
                ORDER BY branches.name
                """
            )

        db.close()
        database_info = {
            "storage": "PostgreSQL" if str(app.config.get("DATABASE", "")).startswith("postgresql") else "SQLite",
            "path": app.config.get("DATABASE", "unknown"),
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
            subject_chart_labels=subject_chart_labels,
            subject_chart_percentages=subject_chart_percentages,
            trend_labels=trend_labels,
            trend_percentages=trend_percentages,
            branch_data=branch_data,
            database_info=database_info,
            mail_info=mail_info,
        )
    except Exception as e:
        print(f"[dashboard] CRITICAL ERROR: {repr(e)}")
        print(traceback.format_exc())
        flash("Dashboard is temporarily unavailable due to a database error.", "error")
        return render_template(
            "dashboard.html",
            error_mode=True,
            branch_count=0,
            student_count=0,
            subject_count=0,
            attendance_count=0,
            total_classes=0,
            present_count=0,
            absent_count=0,
            overall_percentage=0,
            subject_chart_labels=[],
            subject_chart_percentages=[],
            trend_labels=[],
            trend_percentages=[],
            branch_data=[],
            database_info={"storage": "Unknown", "path": "Unavailable"},
            mail_info={
                "configured": False,
                "server": "Unavailable",
                "port": "-",
                "username": None,
                "tls": False,
            },
        )
    finally:
        if db:
            try: db.close()
            except: pass


@app.route("/department-dashboard")
@login_required
@admin_required
def department_dashboard():
    db = None
    try:
        db = get_db()
        departments = db.execute("SELECT id, name, location FROM branches ORDER BY name").fetchall()
        total_students = db.execute("SELECT COUNT(*) AS count FROM students").fetchone()
        total_subjects = db.execute("SELECT COUNT(*) AS count FROM subjects").fetchone()
        total_attendance = db.execute("SELECT COUNT(*) AS count FROM attendance").fetchone()

        attendance_stats = db.execute("""
            SELECT
                SUM(CASE WHEN status = 'Present' THEN 1 ELSE 0 END) AS present_count,
                COUNT(*) AS total_count
            FROM attendance
        """).fetchone()

        overall_percentage = 0
        total_count = row_get(attendance_stats, "total_count", 0) or 0
        present_count = row_get(attendance_stats, "present_count", 0) or 0
        if total_count:
            overall_percentage = round((present_count / total_count) * 100, 1)

        student_counts = {
            row_get(row, "branch_id"): row_get(row, "count", 0) or 0
            for row in db.execute("SELECT branch_id, COUNT(*) AS count FROM students GROUP BY branch_id").fetchall()
        }
        subject_counts = {
            row_get(row, "branch_id"): row_get(row, "count", 0) or 0
            for row in db.execute("SELECT branch_id, COUNT(*) AS count FROM subjects GROUP BY branch_id").fetchall()
        }

        attendance_counts, present_counts, absent_counts = {}, {}, {}
        for row in db.execute("""
            SELECT
                branch_id,
                SUM(CASE WHEN status = 'Present' THEN 1 ELSE 0 END) AS present_count,
                SUM(CASE WHEN status = 'Absent' THEN 1 ELSE 0 END) AS absent_count,
                COUNT(*) AS total_count
            FROM attendance
            GROUP BY branch_id
        """).fetchall():
            branch_id = row_get(row, "branch_id")
            present_counts[branch_id] = row_get(row, "present_count", 0) or 0
            absent_counts[branch_id] = row_get(row, "absent_count", 0) or 0
            attendance_counts[branch_id] = row_get(row, "total_count", 0) or 0

        subjects_by_branch = {}
        for row in db.execute("""
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
        """).fetchall():
            branch_id = row_get(row, "branch_id")
            total = row_get(row, "total_count", 0) or 0
            present = row_get(row, "present_count", 0) or 0
            pct = round((present / total) * 100, 1) if total else 0
            subjects_by_branch.setdefault(branch_id, []).append({
                "id": row_get(row, "subject_id"),
                "name": row_get(row, "subject_name"),
                "present_count": present,
                "total_count": total,
                "pct": pct,
            })

        students_by_branch = {}
        for row in db.execute("""
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
        """).fetchall():
            branch_id = row_get(row, "branch_id")
            total = row_get(row, "total_count", 0) or 0
            present = row_get(row, "present_count", 0) or 0
            absent = row_get(row, "absent_count", 0) or 0
            pct = round((present / total) * 100, 1) if total else 0
            students_by_branch.setdefault(branch_id, []).append({
                "id": row_get(row, "student_id"),
                "name": row_get(row, "student_name"),
                "enrollment": row_get(row, "enrollment"),
                "email": row_get(row, "email"),
                "present": present,
                "absent": absent,
                "total": total,
                "pct": pct,
            })

        departments_data = []
        for dept in departments:
            dept_id = row_get(dept, "id")
            attendance_total = attendance_counts.get(dept_id, 0)
            present_c = present_counts.get(dept_id, 0)
            absent_c = absent_counts.get(dept_id, 0)
            attendance_pct = round((present_c / attendance_total) * 100, 1) if attendance_total else 0
            departments_data.append({
                "id": dept_id,
                "name": row_get(dept, "name"),
                "location": row_get(dept, "location"),
                "student_count": student_counts.get(dept_id, 0),
                "subject_count": subject_counts.get(dept_id, 0),
                "attendance_count": attendance_total,
                "present_count": present_c,
                "absent_count": absent_c,
                "attendance_pct": attendance_pct,
                "subjects": subjects_by_branch.get(dept_id, []),
                "students": students_by_branch.get(dept_id, []),
            })

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
    except Exception as e:
        print(f"[department_dashboard] ERROR: {repr(e)}")
        print(traceback.format_exc())
        flash("Summary page is temporarily unavailable.", "error")
        return redirect(url_for("dashboard"))
    finally:
        if db:
            try: db.close()
            except: pass

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
    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        if request.method == "POST":
            name = request.form.get("name")
            location = request.form.get("location")
            if name:
                db.execute(f"INSERT INTO branches (name, location) VALUES ({placeholder}, {placeholder})", (name, location))
                db.commit()
                flash("Branch added successfully.", "success")
            else:
                flash("Branch name is required.", "error")

        branches_list = db.execute("SELECT * FROM branches ORDER BY name").fetchall()
        return render_template("branches.html", branches=branches_list)
    except Exception as e:
        print(f"[branches] ERROR: {repr(e)}")
        flash("Branch management is temporarily unavailable.", "error")
        return redirect(url_for("dashboard"))
    finally:
        if db:
            try: db.close()
            except: pass


@app.route("/subjects", methods=["GET", "POST"])
@login_required
def subjects():
    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        if request.method == "POST":
            name = request.form.get("name")
            branch_id = request.form.get("branch_id")
            if name and branch_id:
                db.execute(f"INSERT INTO subjects (name, branch_id) VALUES ({placeholder}, {placeholder})", (name, branch_id))
                db.commit()
                flash("Subject added successfully.", "success")
            else:
                flash("Name and branch are required.", "error")

        subjects_list = db.execute("SELECT subjects.*, branches.name AS branch_name FROM subjects JOIN branches ON subjects.branch_id = branches.id ORDER BY subjects.name").fetchall()
        branches_list = db.execute("SELECT id, name FROM branches ORDER BY name").fetchall()
        return render_template("subjects.html", subjects=subjects_list, branches=branches_list)
    except Exception as e:
        print(f"[subjects] ERROR: {repr(e)}")
        flash("Subject management is temporarily unavailable.", "error")
        return redirect(url_for("dashboard"))
    finally:
        if db:
            try: db.close()
            except: pass


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
            # Read the file ONCE into memory to save RAM
            df_full = pd.read_excel(file, header=None)
            
            if df_full.empty:
                flash("The Excel file is empty.", "error")
                return redirect(url_for("upload_students"))

            # Find the header row by searching for keywords in the first 50 rows
            header_idx = 0
            found_header = False
            for i, row in df_full.head(50).iterrows():
                row_str = " ".join([str(cell).lower() for cell in row])
                if any(k in row_str for k in ["name", "enrollment", "h.t.no", "mail", "branch", "section"]):
                    header_idx = i
                    found_header = True
                    break
            
            # Slice the existing dataframe instead of re-reading from disk
            # This is much more memory-efficient on small servers
            df = df_full.iloc[header_idx + 1:].copy()
            df.columns = [str(c).strip() for c in df_full.iloc[header_idx].tolist()]
            
            # Explicitly clear the full dataframe from memory
            del df_full

            if df.empty:
                flash("No data found after the header row.", "error")
                return redirect(url_for("upload_students"))
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


@app.route("/upload_students_csv", methods=["GET", "POST"])
@login_required
@admin_required
def upload_students_csv():
    """
    CSV upload for students with optimized performance, memory efficiency, and production safety.
    
    Features:
    - Streams CSV data to prevent memory exhaustion
    - Validates all fields before database operations
    - Single database commit after all rows processed
    - Detailed logging for debugging
    - Graceful error handling (continues on row errors)
    - Prevents duplicate enrollment insertions
    - Limits file size to prevent worker timeout
    - Compatible with both PostgreSQL and SQLite
    """
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not str(file.filename).lower().endswith(".csv"):
            flash("Please upload a valid CSV file.", "error")
            return redirect(url_for("upload_students_csv"))

        # ============================================================================
        # STEP 1: Validate file size to prevent worker timeout
        # ============================================================================
        max_size_mb = 50  # Limit to 50MB for Render compatibility
        try:
            # Get file size by seeking to end
            file.seek(0, 2)
            file_size = file.tell()
            file.seek(0)
            
            if file_size > max_size_mb * 1024 * 1024:
                flash(f"File is too large ({file_size / 1024 / 1024:.1f}MB). Maximum is {max_size_mb}MB.", "error")
                return redirect(url_for("upload_students_csv"))
            print(f"[CSV Upload] File size OK: {file_size / 1024:.1f}KB")
        except Exception as size_error:
            print(f"[CSV Upload] Could not check file size: {repr(size_error)}")

        db = None
        try:
            import csv
            import io
            import re

            # ====================================================================
            # Helper: Normalize header names (case-insensitive, ignore whitespace)
            # ====================================================================
            def _canon_header(value: object) -> str:
                """Canonicalize header: remove BOM, spaces, underscores, hyphens."""
                text = str(value).lstrip("\ufeff").strip().lower()
                text = re.sub(r"[\s_\-]+", "", text)
                return text

            # ====================================================================
            # Helper: Clean cell values (strip whitespace)
            # ====================================================================
            def _clean_cell(value: object) -> str:
                """Convert value to string and strip whitespace."""
                if value is None or (isinstance(value, float) and pd.isna(value)) if 'pd' in dir() else False:
                    return ""
                text = str(value).strip()
                # Skip common "nan" markers from Excel
                if text.lower() in ("nan", "none", "n/a", ""):
                    return ""
                return text

            # ====================================================================
            # Helper: Validate email format
            # ====================================================================
            def _is_valid_email(email: str) -> bool:
                """Simple email validation."""
                if not email or not "@" in email:
                    return False
                email = email.strip()
                pattern = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
                return re.match(pattern, email) is not None

            # ====================================================================
            # STEP 2: Detect CSV dialect (comma, semicolon, tab, pipe)
            # ====================================================================
            dialect = csv.excel
            try:
                stream = file.stream
                if hasattr(stream, "seek"):
                    stream.seek(0)
                # Read small sample for dialect detection (4KB max)
                sample_bytes = stream.read(4096)
                if hasattr(stream, "seek"):
                    stream.seek(0)
                sample_text = sample_bytes.decode("utf-8-sig", errors="replace")
                dialect = csv.Sniffer().sniff(sample_text, delimiters=",;\t|")
                print(f"[CSV Upload] Detected delimiter: {repr(dialect.delimiter)}")
            except Exception as sniff_error:
                print(f"[CSV Upload] Delimiter detection failed: {repr(sniff_error)}. Using comma.")
                try:
                    if hasattr(file.stream, "seek"):
                        file.stream.seek(0)
                except Exception:
                    pass

            # ====================================================================
            # STEP 3: Open CSV stream and read headers
            # ====================================================================
            text_stream = io.TextIOWrapper(file.stream, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text_stream, dialect=dialect)

            fieldnames = reader.fieldnames or []
            print(f"[CSV Upload] Raw headers detected: {fieldnames}")
            
            if not fieldnames:
                flash("The uploaded CSV file is empty or unreadable.", "error")
                return redirect(url_for("upload_students_csv"))

            # ====================================================================
            # STEP 4: Map CSV headers to required columns (case-insensitive)
            # ====================================================================
            col_map = {}
            for header in fieldnames:
                key = _canon_header(header)
                if key and key not in col_map:
                    col_map[key] = header

            print(f"[CSV Upload] Canonicalized header map: {col_map}")

            # Require: name, enrollment, email, branch_id
            required = {
                "name": "name",
                "enrollment": "enrollment",
                "email": "email",
                "branch_id": "branchid",
            }
            missing = [label for label, key in required.items() if key not in col_map]
            if missing:
                flash(
                    f"Missing required columns: {', '.join(missing)}. "
                    "CSV must include: Name, Enrollment, Email, and Branch_ID.",
                    "error"
                )
                return redirect(url_for("upload_students_csv"))

            print(f"[CSV Upload] All required columns found. Starting row processing...")

            # ====================================================================
            # STEP 5: Initialize database and counters
            # ====================================================================
            db = get_db()
            placeholder = get_placeholder()
            is_postgres = str(app.config.get("DATABASE", "")).startswith("postgres")

            inserted = 0
            skipped = 0
            failed = 0
            seen_enrollments = set()
            failed_rows_log = []

            # ====================================================================
            # STEP 6: Pre-fetch valid branch IDs to avoid repeated DB queries
            # ====================================================================
            valid_branch_ids = set()
            try:
                for b_row in db.execute("SELECT id FROM branches").fetchall():
                    try:
                        bid = row_get(b_row, "id")
                        if bid:
                            valid_branch_ids.add(int(bid))
                    except Exception:
                        pass
                print(f"[CSV Upload] Valid branch IDs: {sorted(valid_branch_ids)}")
            except Exception as branch_error:
                print(f"[CSV Upload] ERROR fetching branches: {repr(branch_error)}")
                flash("Failed to fetch branch list. Check database.", "error")
                return redirect(url_for("upload_students_csv"))

            # ====================================================================
            # STEP 7: Process each row with individual error handling
            # ====================================================================
            for row_number, row in enumerate(reader, start=2):  # Start at 2 (header is row 1)
                try:
                    # Log current row for debugging
                    print(f"[CSV Upload] Processing row {row_number}: {dict(row)}")

                    # ============================================================
                    # Extract and clean values
                    # ============================================================
                    name = _clean_cell(row.get(col_map[required["name"]], ""))
                    enrollment = _clean_cell(row.get(col_map[required["enrollment"]], ""))
                    email = _clean_cell(row.get(col_map[required["email"]], ""))
                    branch_id_raw = _clean_cell(row.get(col_map[required["branch_id"]], ""))

                    # ============================================================
                    # Skip completely empty rows
                    # ============================================================
                    if not name and not enrollment and not email and not branch_id_raw:
                        print(f"[CSV Upload] Row {row_number}: Skipped (completely empty)")
                        skipped += 1
                        continue

                    # ============================================================
                    # Validate required fields are not empty
                    # ============================================================
                    if not name:
                        msg = f"Row {row_number}: Name is required"
                        print(f"[CSV Upload] {msg}")
                        failed_rows_log.append(msg)
                        failed += 1
                        continue

                    if not enrollment:
                        msg = f"Row {row_number}: Enrollment is required"
                        print(f"[CSV Upload] {msg}")
                        failed_rows_log.append(msg)
                        failed += 1
                        continue

                    if not email:
                        msg = f"Row {row_number}: Email is required"
                        print(f"[CSV Upload] {msg}")
                        failed_rows_log.append(msg)
                        failed += 1
                        continue

                    if not branch_id_raw:
                        msg = f"Row {row_number}: Branch_ID is required"
                        print(f"[CSV Upload] {msg}")
                        failed_rows_log.append(msg)
                        failed += 1
                        continue

                    # ============================================================
                    # Validate branch_id is a valid integer
                    # ============================================================
                    try:
                        branch_id = int(branch_id_raw)
                    except (ValueError, TypeError):
                        msg = f"Row {row_number}: Invalid branch_id '{branch_id_raw}' (not an integer)"
                        print(f"[CSV Upload] {msg}")
                        failed_rows_log.append(msg)
                        failed += 1
                        continue

                    # ============================================================
                    # Validate branch_id exists in database
                    # ============================================================
                    if branch_id not in valid_branch_ids:
                        msg = f"Row {row_number}: Branch_ID {branch_id} does not exist"
                        print(f"[CSV Upload] {msg}")
                        failed_rows_log.append(msg)
                        failed += 1
                        continue

                    # ============================================================
                    # Validate email format
                    # ============================================================
                    if not _is_valid_email(email):
                        msg = f"Row {row_number}: Invalid email format '{email}'"
                        print(f"[CSV Upload] {msg}")
                        failed_rows_log.append(msg)
                        failed += 1
                        continue

                    # ============================================================
                    # Check for duplicate enrollment in CSV (same file)
                    # ============================================================
                    enrollment_key = enrollment.lower()
                    if enrollment_key in seen_enrollments:
                        msg = f"Row {row_number}: Duplicate enrollment in CSV '{enrollment}'"
                        print(f"[CSV Upload] {msg}")
                        failed_rows_log.append(msg)
                        failed += 1
                        continue
                    seen_enrollments.add(enrollment_key)

                    # ============================================================
                    # Check for duplicate enrollment in database
                    # ============================================================
                    existing = db.execute(
                        f"SELECT id FROM students WHERE enrollment = {placeholder}",
                        (enrollment,),
                    ).fetchone()
                    if existing:
                        msg = f"Row {row_number}: Enrollment '{enrollment}' already exists in database"
                        print(f"[CSV Upload] {msg}")
                        failed_rows_log.append(msg)
                        failed += 1
                        continue

                    # ============================================================
                    # Insert student (with conflict handling)
                    # ============================================================
                    student_id = None
                    if is_postgres:
                        cur = db.execute(
                            f"""
                            INSERT INTO students (name, enrollment, email, branch_id)
                            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                            ON CONFLICT (enrollment) DO NOTHING
                            RETURNING id
                            """,
                            (name, enrollment, email, branch_id),
                        )
                        result = cur.fetchone()
                        if not result:
                            msg = f"Row {row_number}: Failed to insert student (enrollment conflict?)"
                            print(f"[CSV Upload] {msg}")
                            failed_rows_log.append(msg)
                            failed += 1
                            continue
                        student_id = row_get(result, "id") or result[0]
                    else:
                        # SQLite: Use INSERT OR IGNORE
                        cur = db.execute(
                            f"""
                            INSERT OR IGNORE INTO students (name, enrollment, email, branch_id)
                            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                            """,
                            (name, enrollment, email, branch_id),
                        )
                        if getattr(cur, "rowcount", 0) == 0:
                            msg = f"Row {row_number}: Failed to insert student (enrollment conflict?)"
                            print(f"[CSV Upload] {msg}")
                            failed_rows_log.append(msg)
                            failed += 1
                            continue
                        student_id = getattr(cur, "lastrowid", None)

                    if not student_id:
                        msg = f"Row {row_number}: Could not retrieve student ID after insert"
                        print(f"[CSV Upload] {msg}")
                        failed_rows_log.append(msg)
                        failed += 1
                        continue

                    # ============================================================
                    # Create user account for student
                    # ============================================================
                    password_plain = enrollment[-4:] if len(enrollment) >= 4 else enrollment
                    if is_postgres:
                        db.execute(
                            f"""
                            INSERT INTO users (username, password, role, student_id)
                            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                            ON CONFLICT (username) DO NOTHING
                            """,
                            (enrollment, generate_password_hash(password_plain), "student", student_id),
                        )
                    else:
                        db.execute(
                            f"""
                            INSERT OR IGNORE INTO users (username, password, role, student_id)
                            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                            """,
                            (enrollment, generate_password_hash(password_plain), "student", student_id),
                        )

                    inserted += 1
                    print(f"[CSV Upload] Row {row_number}: SUCCESS (student_id={student_id})")

                except Exception as row_error:
                    # ============================================================
                    # Handle unexpected row errors without crashing
                    # ============================================================
                    failed += 1
                    msg = f"Row {row_number}: {repr(row_error)}"
                    print(f"[CSV Upload] EXCEPTION: {msg}")
                    print(traceback.format_exc())
                    failed_rows_log.append(msg)
                    continue

            # ====================================================================
            # STEP 8: Commit all changes at once (SINGLE COMMIT)
            # ====================================================================
            print(f"[CSV Upload] All rows processed. Committing {inserted} insertions...")
            db.commit()

            # ====================================================================
            # STEP 9: Build upload summary and display to user
            # ====================================================================
            summary = f"Upload complete! {inserted} students added, {skipped} skipped, {failed} failed."
            print(f"[CSV Upload] {summary}")
            
            if failed_rows_log:
                # Show first 5 failed rows to user
                failed_preview = "\n".join(failed_rows_log[:5])
                if len(failed_rows_log) > 5:
                    failed_preview += f"\n... and {len(failed_rows_log) - 5} more errors"
                print(f"[CSV Upload] Failed rows:\n{failed_preview}")
                flash(f"{summary}\n\nFailed rows:\n{failed_preview}", "warning")
            else:
                flash(summary, "success")

            return redirect(url_for("students"))

        except Exception as e:
            # ====================================================================
            # STEP 10: Global error handling (never crash the app)
            # ====================================================================
            print(f"[CSV Upload CRITICAL] Unhandled exception: {repr(e)}")
            print(traceback.format_exc())
            
            if db:
                try:
                    db.rollback()
                    print("[CSV Upload] Database rolled back after critical error")
                except Exception as rollback_error:
                    print(f"[CSV Upload] Rollback failed: {repr(rollback_error)}")

            flash(
                "Failed to process CSV file due to a critical error. "
                "Please check the file format and try again. See server logs for details.",
                "error"
            )
            return redirect(url_for("upload_students_csv"))

        finally:
            # ====================================================================
            # STEP 11: Cleanup (close database connection)
            # ====================================================================
            if db:
                try:
                    db.close()
                    print("[CSV Upload] Database connection closed")
                except Exception:
                    pass

    return render_template("upload_students_csv.html")


@app.route("/students", methods=["GET", "POST"])
@login_required
def students():
    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            enrollment = request.form.get("enrollment", "").strip()
            branch_id = request.form.get("branch_id", "").strip()
            email = request.form.get("email", "").strip()

            if not name or not enrollment or not branch_id:
                flash("Name, enrollment, and branch are required.", "error")
            elif email and not is_valid_email(email):
                flash("Please enter a valid email address.", "error")
            else:
                existing = db.execute(f"SELECT id FROM students WHERE enrollment = {placeholder}", (enrollment,)).fetchone()
                if existing:
                    flash("A student with this enrollment already exists.", "error")
                else:
                    try:
                        if str(app.config.get("DATABASE", "")).startswith("postgres"):
                            cur = db.execute(f"INSERT INTO students (name, enrollment, email, branch_id) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}) RETURNING id", (name, enrollment, email or None, branch_id))
                            student_id = cur.fetchone()[0]
                        else:
                            cur = db.execute(f"INSERT INTO students (name, enrollment, email, branch_id) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})", (name, enrollment, email or None, branch_id))
                            student_id = cur.lastrowid
                        
                        db.execute(f"INSERT INTO users (username, password, role, student_id) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})", (enrollment, generate_password_hash(enrollment[-4:]), "student", student_id))
                        db.commit()
                        flash("Student added successfully.", "success")
                    except Exception as e:
                        db.rollback()
                        flash(f"Error adding student: {repr(e)}", "error")

        search = request.args.get("search", "").strip()
        branch_filter = request.args.get("branch_id", "").strip()
        
        query = "SELECT students.*, branches.name AS branch_name FROM students JOIN branches ON students.branch_id = branches.id"
        clauses, params = [], []
        if search:
            like_op = "ILIKE" if str(app.config.get("DATABASE", "")).startswith("postgres") else "LIKE"
            clauses.append(f"(students.name {like_op} {placeholder} OR students.enrollment {like_op} {placeholder})")
            params.extend([f"%{search}%", f"%{search}%"])
        if branch_filter:
            clauses.append(f"students.branch_id = {placeholder}")
            params.append(branch_filter)
        if clauses: query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY students.name"
        
        students_list = db.execute(query, params).fetchall()
        branches_list = db.execute("SELECT id, name FROM branches ORDER BY name").fetchall()
        return render_template("students.html", students=students_list, branches=branches_list)
    except Exception as e:
        print(f"[students] ERROR: {repr(e)}")
        flash("Student management is temporarily unavailable.", "error")
        return redirect(url_for("dashboard"))
    finally:
        if db:
            try: db.close()
            except: pass


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
@app.route("/student/dashboard")
@login_required
def student_dashboard():
    student_id = session.get("student_id")
    if not student_id:
        flash("Student record not found.", "error")
        return redirect(url_for("student_login"))

    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        student = db.execute(f"SELECT students.*, branches.name AS branch_name FROM students JOIN branches ON students.branch_id = branches.id WHERE students.id = {placeholder}", (student_id,)).fetchone()
        
        if not student:
            db.close()
            abort(404)

        selected_subject_id = request.args.get("subject_id") or ""
        subjects = db.execute(f"SELECT id, name FROM subjects WHERE branch_id = {placeholder} ORDER BY name", (row_get(student, "branch_id"),)).fetchall()
        
        attendance_query = f"SELECT attendance.date, attendance.status, subjects.name AS subject_name, subjects.id AS subject_id FROM attendance JOIN subjects ON attendance.subject_id = subjects.id WHERE attendance.student_id = {placeholder} "
        params = [student_id]
        if selected_subject_id:
            attendance_query += f"AND attendance.subject_id = {placeholder} "
            params.append(selected_subject_id)
        attendance_query += "ORDER BY attendance.date DESC"
        
        attendance_records = db.execute(attendance_query, tuple(params)).fetchall()
        total = len(attendance_records)
        present = len([a for a in attendance_records if row_get(a, "status") == "Present"])
        absent = total - present
        percentage = round((present / total) * 100, 1) if total > 0 else 0

        student_qr_data_uri = None
        try:
            import base64, qrcode
            from io import BytesIO
            enrollment = str(row_get(student, "enrollment") or "")
            payload = f"ENROLLMENT:{enrollment}" if enrollment else f"STUDENT_ID:{student_id}"
            qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=2)
            qr.add_data(payload)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            student_qr_data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        except: pass

        db.close()
        return render_template("student_dashboard.html", student=student, attendance_records=attendance_records, total_classes=total, present_count=present, absent_count=absent, percentage=percentage, subjects=subjects, selected_subject_id=selected_subject_id, student_qr_data_uri=student_qr_data_uri)
    except Exception as e:
        print(f"[student_dashboard] ERROR: {repr(e)}")
        if db:
            try: db.close()
            except: pass
        flash("Your dashboard is temporarily unavailable.", "error")
        return redirect(url_for("student_login"))


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


@app.route("/mark_attendance", methods=["GET", "POST"])
@login_required
def mark_attendance():
    """Clean, high-performance route to mark student attendance."""
    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        today_str = date.today().isoformat()
        
        # 1. Fetch initial selection data
        branches = db.execute("SELECT id, name FROM branches ORDER BY name").fetchall()
        branch_id = request.args.get("branch_id")
        subject_id = request.args.get("subject_id")
        selected_date = request.args.get("date") or today_str

        # 2. Handle POST (Saving Attendance)
        if request.method == "POST":
            # Re-read form data to avoid stale context
            branch_id = request.form.get("branch_id")
            subject_id = request.form.get("subject_id")
            selected_date = request.form.get("date") or today_str
            student_ids = request.form.getlist("student_id")
            
            if branch_id and subject_id and student_ids:
                try:
                    saved_ids = []
                    for student_id in student_ids:
                        status = request.form.get(f"status_{student_id}", "Absent")
                        note = request.form.get(f"note_{student_id}", "")
                        
                        # Use ON CONFLICT for PostgreSQL stability, or manual check for SQLite
                        is_pg = str(app.config.get("DATABASE", "")).startswith("postgres")
                        if is_pg:
                            db.execute(f"""
                                INSERT INTO attendance (student_id, branch_id, subject_id, date, status, note)
                                VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
                                ON CONFLICT (student_id, subject_id, date) DO UPDATE 
                                SET status = EXCLUDED.status, note = EXCLUDED.note
                            """, (student_id, branch_id, subject_id, selected_date, status, note))
                        else:
                            # SQLite manual update
                            db.execute(f"DELETE FROM attendance WHERE student_id={placeholder} AND subject_id={placeholder} AND date={placeholder}", (student_id, subject_id, selected_date))
                            db.execute(f"INSERT INTO attendance (student_id, branch_id, subject_id, date, status, note) VALUES ({placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder})", (student_id, branch_id, subject_id, selected_date, status, note))
                        
                        if str(student_id).isdigit():
                            saved_ids.append(int(student_id))
                    
                    db.commit()
                    flash("Attendance saved successfully.", "success")
                    notify_low_attendance(db, saved_ids) # Background notification
                    return redirect(url_for("attendance_success", branch_id=branch_id, subject_id=subject_id, date=selected_date))
                except Exception as e:
                    db.rollback()
                    print(f"[mark_attendance] Save Error: {repr(e)}")
                    flash("Failed to save attendance. Please check your data.", "error")

        # 3. Fetch data for display
        subjects = []
        if branch_id:
            subjects = db.execute(f"SELECT id, name FROM subjects WHERE branch_id = {placeholder} ORDER BY name", (branch_id,)).fetchall()

        students = []
        attendance_map = {}
        if branch_id and subject_id:
            students = db.execute(f"SELECT id, name, enrollment FROM students WHERE branch_id = {placeholder} ORDER BY name", (branch_id,)).fetchall()
            att_rows = db.execute(f"SELECT student_id, status, note FROM attendance WHERE subject_id = {placeholder} AND date = {placeholder}", (subject_id, selected_date)).fetchall()
            attendance_map = {str(row_get(r, "student_id")): r for r in att_rows}

        return render_template(
            "mark_attendance.html",
            branches=branches,
            subjects=subjects,
            students=students,
            branch_id=branch_id,
            subject_id=subject_id,
            selected_date=selected_date,
            attendance_map=attendance_map,
            today_date=today_str
        )
    except Exception as e:
        print(f"[mark_attendance] General Error: {repr(e)}")
        flash("Unable to load attendance page.", "error")
        return redirect(url_for("dashboard"))
    finally:
        if db:
            try: db.close()
            except: pass


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
        (branch_id,)
    ).fetchone()
    subject = db.execute(
        f"SELECT name FROM subjects WHERE id = {placeholder}",
        (subject_id,)
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
        (branch_id,)
    ).fetchone()
    subject = db.execute(
        f"SELECT name FROM subjects WHERE id = {placeholder}",
        (subject_id,)
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


def build_report_pdf(records):
    """Build a simple PDF attendance report with professional table styling."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.units import mm
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except Exception as e:
        raise RuntimeError("PDF export requires reportlab. Add reportlab to requirements.txt") from e

    output = BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )
    styles = getSampleStyleSheet()

    table_data = [["Name", "Enrollment", "Subject", "Date", "Status"]]
    for r in records:
        table_data.append(
            [
                str(row_get(r, "student_name") or ""),
                str(row_get(r, "enrollment") or ""),
                str(row_get(r, "subject_name") or ""),
                str(row_get(r, "date") or ""),
                str(row_get(r, "status") or ""),
            ]
        )

    elements = [
        Paragraph("Attendance Report", styles["Title"]),
        Paragraph(f"Generated on: {date.today().isoformat()}", styles["Normal"]),
        Spacer(1, 8),
    ]

    table = Table(table_data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4361ee")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d9dce3")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fc")]),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
                ("TOPPADDING", (0, 0), (-1, 0), 8),
            ]
        )
    )
    elements.append(table)
    doc.build(elements)
    output.seek(0)
    filename = f"attendance_report_{date.today().isoformat()}.pdf"
    return output.getvalue(), filename


@app.route("/download_attendance")
@login_required
def download_attendance():
    """Download attendance as an Excel file. Robust and memory efficient."""
    db = None
    try:
        db = get_db()
        filters = {
            "branch_id": request.args.get("branch_id"),
            "subject_id": request.args.get("subject_id"),
            "from_date": request.args.get("from_date"),
            "to_date": request.args.get("to_date")
        }
        
        if session.get("role") == "student":
            filters["student_id"] = session.get("student_id")

        records = fetch_report_records(db, filters)
        
        rows = []
        for r in records:
            rows.append({
                "Date": row_get(r, "date"),
                "Student": row_get(r, "student_name"),
                "Subject": row_get(r, "subject_name"),
                "Branch": row_get(r, "branch_name"),
                "Status": row_get(r, "status")
            })

        import pandas as pd
        df = pd.DataFrame(rows)
        if df.empty:
            df = pd.DataFrame(columns=["Date", "Student", "Subject", "Branch", "Status"])

        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Attendance")
        output.seek(0)

        db.close()
        return send_file(
            output,
            as_attachment=True,
            download_name=f"attendance_report_{date.today()}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        print(f"[DOWNLOAD] Error: {repr(e)}")
        if db:
            try: db.close()
            except: pass
        flash("Failed to generate Excel report.", "error")
        return redirect(url_for("dashboard"))


@app.route("/attendance/report")
@login_required
def attendance_report():
    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        filters = {
            "branch_id": request.args.get("branch_id"),
            "subject_id": request.args.get("subject_id"),
            "student_id": request.args.get("student_id"),
            "from_date": request.args.get("from_date"),
            "to_date": request.args.get("to_date"),
            "search": request.args.get("search"),
        }
        
        records = fetch_report_records(db, filters)
        stats = build_report_stats(records)
        
        branches = db.execute("SELECT id, name FROM branches ORDER BY name").fetchall()
        subjects = db.execute("SELECT id, name FROM subjects ORDER BY name").fetchall()
        students = db.execute("SELECT id, name FROM students ORDER BY name").fetchall()
        
        return render_template("attendance_report.html", records=records, stats=stats, filters=filters, branches=branches, subjects=subjects, students=students)
    except Exception as e:
        print(f"[attendance_report] ERROR: {repr(e)}")
        flash("Reports are temporarily unavailable.", "error")
        return redirect(url_for("dashboard"))
    finally:
        if db:
            try: db.close()
            except: pass


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
    except Exception as e:
        print(f"[export_excel] ERROR: {repr(e)}")
        flash("Failed to export Excel report.", "error")
        return redirect(url_for("attendance_report", **{k: v for k, v in get_report_filters().items() if v}))
    finally:
        try:
            db.close()
        except Exception:
            pass


@app.route("/reports/export/pdf")
@login_required
def export_pdf():
    db = get_db()
    filters = get_report_filters()
    redirect_params = {k: v for k, v in filters.items() if v}
    try:
        records = fetch_report_records(db, filters)
        content, filename = build_report_pdf(records)
        return send_file(
            BytesIO(content),
            as_attachment=True,
            download_name=filename,
            mimetype="application/pdf",
        )
    except Exception as e:
        print(f"[export_pdf] ERROR: {repr(e)}")
        flash("Failed to export PDF report. Ensure reportlab is installed.", "error")
        return redirect(url_for("attendance_report", **redirect_params))
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
                (filters["branch_id"],)
            ).fetchone()
            branch_name = row_get(branch_row, "name") or branch_name

        if filters.get("subject_id"):
            subject_row = db.execute(
                f"SELECT name FROM subjects WHERE id = {placeholder}",
                (filters["subject_id"],)
            ).fetchone()
            subject_name = row_get(subject_row, "name") or subject_name

        if filters.get("student_id"):
            student_row = db.execute(
                f"SELECT name FROM students WHERE id = {placeholder}",
                (filters["student_id"],)
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

        html_summary = (
            "<div style='font-family:Arial,sans-serif;line-height:1.6;color:#1f2937'>"
            "<h2 style='margin-bottom:8px;color:#4361ee'>Attendance Report</h2>"
            f"<p><strong>Branch:</strong> {branch_name}<br>"
            f"<strong>Subject:</strong> {subject_name}<br>"
            f"<strong>Student:</strong> {student_name}<br>"
            f"<strong>Total records:</strong> {stats.get('total_records', 0)}<br>"
            f"<strong>Overall attendance:</strong> {stats.get('overall_percentage', 0)}%</p>"
            "<p>The detailed report is attached as an Excel file.</p>"
            "</div>"
        )

        email_sent = safe_send_email(
            subject="Attendance Report",
            recipient=recipient,
            body="\n".join(body_lines),
            html_body=html_summary,
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
    except Exception as e:
        print(f"[report_email] ERROR: {repr(e)}")
        print(traceback.format_exc())
        flash("Failed to build or send the report email.", "error")
    finally:
        try:
            db.close()
        except Exception:
            pass

    return redirect(url_for("attendance_report", **redirect_params))


# SocketIO Event Handlers for Real-time Updates
@socketio.on('connect')
def handle_connect():
    print("Client connected")
    emit('status', {'message': 'Connected to real-time system'})

@socketio.on('disconnect')
def handle_disconnect():
    print("Client disconnected")

@socketio.on('join_room')
def handle_join_room(data):
    room = data.get('room')
    if room:
        join_room(room)
        print(f"Client joined room: {room}")

@socketio.on('request_stats')
def handle_request_stats():
    """Fetch and emit latest stats to the dashboard."""
    db = None
    try:
        db = get_db()
        stats = {
            'total_records': db.execute("SELECT COUNT(*) FROM attendance").fetchone()[0],
            'overall_percentage': 0,
            'recent_activity': []
        }
        
        total = db.execute("SELECT COUNT(*) FROM attendance").fetchone()[0]
        present = db.execute("SELECT COUNT(*) FROM attendance WHERE status='Present'").fetchone()[0]
        if total > 0:
            stats['overall_percentage'] = round((present / total) * 100, 1)
            
        # Get last 5 activity items
        activity_rows = db.execute("""
            SELECT a.date, s.name as student_name, sub.name as subject_name, a.status, b.name as branch_name
            FROM attendance a
            JOIN students s ON a.student_id = s.id
            JOIN subjects sub ON a.subject_id = sub.id
            JOIN branches b ON a.branch_id = b.id
            ORDER BY a.id DESC LIMIT 5
        """).fetchall()
        
        stats['recent_activity'] = [
            {
                'date': str(r['date']),
                'student': r['student_name'],
                'subject': r['subject_name'],
                'status': r['status'],
                'branch': r['branch_name']
            } for r in activity_rows
        ]
        
        emit('stats_update', stats)
    except Exception as e:
        print(f"SocketIO Stats Error: {e}")
    finally:
        if db: db.close()


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
                html_body = (
                    "<div style='font-family:Arial,sans-serif;line-height:1.6;color:#1f2937'>"
                    "<h2 style='margin-bottom:8px;color:#4361ee'>Reset Your Password</h2>"
                    "<p>We received a request to reset your password.</p>"
                    f"<p><a href='{reset_link}' style='display:inline-block;padding:10px 14px;background:#4361ee;color:#fff;text-decoration:none;border-radius:6px'>Reset Password</a></p>"
                    f"<p>If the button does not work, use this link:<br><a href='{reset_link}'>{reset_link}</a></p>"
                    "<p>If you did not request this, you can ignore this email.</p>"
                    "<p>Regards,<br>Attendance Management Team</p>"
                    "</div>"
                )
                email_sent = safe_send_email(
                    subject="Reset your password",
                    recipient=student_email,
                    body=body,
                    html_body=html_body,
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

@app.route("/admin/notify-low-attendance", methods=["POST"])
@login_required
def trigger_low_attendance_scan():
    """Admin route to scan and notify students with < 75% attendance."""
    if session.get("role") != "admin":
        abort(403)
        
    db = None
    try:
        db = get_db()
        # Query for all students with low attendance
        query = """
            SELECT 
                s.id, s.name, s.email,
                COUNT(a.id) as total,
                SUM(CASE WHEN a.status = 'Present' THEN 1 ELSE 0 END) as present
            FROM students s
            LEFT JOIN attendance a ON s.id = a.student_id
            GROUP BY s.id, s.name, s.email
            HAVING COUNT(a.id) >= 5
        """
        rows = db.execute(query).fetchall()
        count = 0
        for row in rows:
            total = row_get(row, "total")
            present = row_get(row, "present")
            pct = (present / total) * 100
            if pct < 75:
                email = row_get(row, "email")
                if email and is_valid_email(email):
                    subject = "Attendance Warning (<75%)"
                    body = f"Dear {row_get(row, 'name')},\n\nYour current attendance is {round(pct, 1)}%. This is below the required 75% threshold.\n\nPlease attend classes regularly.\n\nRegards,\nAdministration"
                    if safe_send_email(subject, email, body):
                        count += 1
        
        db.close()
        flash(f"Scan complete. Notified {count} students with low attendance.", "success")
        return redirect(url_for("dashboard"))
    except Exception as e:
        print(f"[ALERT] Error during scan: {repr(e)}")
        if db:
            try: db.close()
            except: pass
        flash("An error occurred during the attendance scan.", "error")
        return redirect(url_for("dashboard"))

@app.errorhandler(500)
def internal_error(error):
    """Global handler for Internal Server Errors."""
    print(f"[CRITICAL] 500 ERROR: {repr(error)}")
    print(traceback.format_exc())
    return "<h1>Internal Server Error</h1><p>Our team has been notified. Please try again later.</p>", 500

@app.errorhandler(404)
def not_found_error(error):
    return "<h1>404 Not Found</h1><p>The page you requested does not exist.</p>", 404

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=10000, debug=True)
