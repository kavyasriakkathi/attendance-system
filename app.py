import os
import logging
import re
import base64
from datetime import date, timedelta
from io import BytesIO
import sqlite3
import time
import threading
import traceback
from typing import Tuple
from urllib.parse import urlparse
import requests
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from dotenv import load_dotenv

from flask import Flask, abort, redirect, render_template, request, session, url_for, flash, jsonify, send_file, make_response
from functools import wraps
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)

# Initialize SocketIO after app creation
# Use threading mode for compatibility with the workspace Python version and tests.
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

# Load .env variables if present (does not overwrite existing environment)
load_dotenv(override=False)

# Suppress itsdangerous warnings about invalid session cookies (expected for old/tampered cookies)
import warnings
warnings.filterwarnings("ignore", category=Warning, module="itsdangerous")
logging.getLogger("itsdangerous").setLevel(logging.ERROR)

# Session cookie and lifetime configuration
# In production (Render) ensure cookies are secure and SECRET_KEY is set.
is_prod = bool(os.environ.get("RENDER") or os.environ.get("RENDER_INTERNAL_HOSTNAME") or os.environ.get("FLASK_ENV", "").lower() == "production")

# Normalize Resend credentials from environment
raw_resend_api_key = os.environ.get("RESEND_API_KEY") or ""
raw_mail_from = os.environ.get("MAIL_FROM") or ""
resend_api_key = raw_resend_api_key.strip() if raw_resend_api_key else None
mail_from = raw_mail_from.strip() if raw_mail_from else None

app.config.from_mapping(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-key-change-in-production"),
    DATABASE=database_path,
    RESEND_API_KEY=resend_api_key,
    MAIL_FROM=mail_from,
    MAIL_PROVIDER="resend",
    # Keep these for compatibility with existing templates/debug cards.
    MAIL_SERVER="api.resend.com",
    MAIL_PORT=443,
    MAIL_USERNAME=mail_from,
    MAIL_USE_TLS=True,
    REPORT_ADMIN_EMAIL=os.environ.get("REPORT_ADMIN_EMAIL", "instituteattendanceapp@gmail.com"),
    LOW_ATTENDANCE_THRESHOLD=int(os.environ.get("LOW_ATTENDANCE_THRESHOLD", 75)),
)


def _mask_env_value(key: str, value: str) -> str:
    """Return a masked representation for sensitive env values.

    We avoid printing full secrets. For API keys and passwords, show a short
    masked summary. For non-sensitive values (emails/usernames), show a
    truncated value for convenience.
    """
    try:
        if value is None:
            return "<not set>"
        s = str(value)
    except Exception:
        return "<unavailable>"
    lower = key.lower()
    # Treat anything with 'key' or 'secret' or 'password' as sensitive
    if any(tok in lower for tok in ("password", "secret", "api_key", "apikey", "token")):
        if len(s) <= 6:
            return "<set>"
        return s[:3] + "..." + s[-3:]
    # For emails and usernames, mask username part leaving domain
    if "@" in s:
        parts = s.split("@")
        user = parts[0]
        if len(user) <= 2:
            masked_user = "*"
        else:
            masked_user = user[0] + "..." + user[-1]
        return masked_user + "@" + parts[1]
    # Fallback: show first/last char
    if len(s) <= 4:
        return s[0] + "..."
    return s[:2] + "..." + s[-2:]


def _log_mail_env_summary():
    """Log which mail-related environment variables are present (masked).

    This runs at module import time so operators see the state in startup
    logs. It respects the fact that `load_dotenv(override=False)` was called,
    meaning Render/production env vars take precedence over .env.
    """
    logger = logging.getLogger("app.mailenv")
    mail_keys = ["MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_FROM", "RESEND_API_KEY", "REPORT_ADMIN_EMAIL"]
    present = []
    missing = []
    lines = []
    for k in mail_keys:
        # Prefer actual environment variables (Render/production). fall back to app.config
        raw = os.environ.get(k)
        if raw is None:
            raw = app.config.get(k)
        if raw and str(raw).strip():
            present.append(k)
            lines.append(f"{k}= { _mask_env_value(k, raw) }")
        else:
            missing.append(k)
            lines.append(f"{k}= <not set>")

    mode = "production" if is_prod else "development/local"
    print(f"[mail.env] Starting mail environment diagnostics ({mode}). .env was loaded with override=False so existing environment vars were preserved.")
    for ln in lines:
        # Use print to ensure visible in stdout logs
        print(f"[mail.env] {ln}")

    if missing:
        print(f"[mail.env] Missing mail vars: {', '.join(missing)}")
    else:
        print("[mail.env] All mail vars detected (masked above).")


# Run mail env summary immediately so it's visible in startup logs
try:
    _log_mail_env_summary()
except Exception as e:
    # Print a sanitized startup traceback so operators can debug why the
    # diagnostics failed while avoiding full secret exposure. Dump only the
    # exception type + first few lines of the traceback.
    try:
        import traceback as _tb
        tb_text = _tb.format_exc()
        # Keep only the first 6 lines to avoid huge logs
        tb_lines = tb_text.splitlines()
        snippet = "\n".join(tb_lines[:6])
        print(f"[mail.env] Failed to run mail env diagnostics: {type(e).__name__}: {str(e)}")
        print("[mail.env] Traceback (sanitized):")
        for ln in snippet.splitlines():
            print(f"[mail.env] {ln}")
    except Exception:
        print("[mail.env] Failed to run mail env diagnostics (unable to format traceback).")
app.config.setdefault("MAX_CONTENT_LENGTH", int(os.environ.get("MAX_CONTENT_LENGTH", 25 * 1024 * 1024)))
# SESSION_COOKIE configuration
app.config.setdefault("SESSION_COOKIE_SECURE", os.environ.get("SESSION_COOKIE_SECURE", "True" if is_prod else "False").lower() in ("true", "1", "yes"))
app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
app.config.setdefault("SESSION_COOKIE_SAMESITE", os.environ.get("SESSION_COOKIE_SAMESITE", "Lax"))
app.config.setdefault("PERMANENT_SESSION_LIFETIME", timedelta(hours=int(os.environ.get("PERMANENT_SESSION_HOURS", "8"))))

# Warn loudly if running in production without a proper SECRET_KEY
if is_prod and app.config.get("SECRET_KEY") in (None, "", "dev-key-change-in-production"):
    print("[SECURITY] WARNING: Running in production without a real SECRET_KEY.\nSet the SECRET_KEY environment variable to a stable secret for all instances.")

# Centralized mail configuration and diagnostics (use mail_config module)
try:
    import mail_config
    # setup_mail_config will populate app.config with MAIL_* keys and print masked diagnostics
    mail_config.setup_mail_config(app)
except Exception as e:
    print(f"[mail.config] Failed to initialize mail_config module: {type(e).__name__}: {str(e)}")

def is_mail_configured():
    try:
        return mail_config.is_mail_configured(app)
    except Exception:
        # Fallback to previous heuristic
        api_key = app.config.get("RESEND_API_KEY")
        from_email = app.config.get("MAIL_FROM")
        return bool(api_key and str(api_key).strip() and from_email and str(from_email).strip())

# Middleware: gracefully handle invalid/unsigned session cookies (itsdangerous.BadSignature)
class _SessionFixMiddleware:
    def __init__(self, app_):
        self.app = app_

    def __call__(self, environ, start_response):
        from itsdangerous import BadSignature
        try:
            return self.app(environ, start_response)
        except BadSignature as e:
            try:
                from werkzeug.wrappers import Response
                # Clear the session cookie and redirect to login page
                res = Response(status=302)
                res.headers["Location"] = "/login"
                cookie_name = app.config.get("SESSION_COOKIE_NAME", "session")
                res.set_cookie(cookie_name, "", expires=0, path='/', secure=app.config.get("SESSION_COOKIE_SECURE"), httponly=app.config.get("SESSION_COOKIE_HTTPONLY"), samesite=app.config.get("SESSION_COOKIE_SAMESITE"))
                print(f"[session] Invalid signed session detected — clearing cookie and redirecting. detail={repr(e)}")
                return res(environ, start_response)
            except Exception:
                # If anything goes wrong here, re-raise the original exception
                raise

app.wsgi_app = _SessionFixMiddleware(app.wsgi_app)

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
                try:
                    cur.execute(query, params)
                except Exception as ex:
                    # If the connection is in a failed transaction state, roll back
                    # and retry once. This prevents InFailedSqlTransaction from
                    # cascading into login, dashboard, and every other route.
                    if "InFailedSqlTransaction" in type(ex).__name__ or "current transaction is aborted" in str(ex):
                        print(f"[DB] Aborted transaction detected — rolling back and retrying.")
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        cur = self._conn.cursor(cursor_factory=DictCursor)
                        cur.execute(query, params)
                    else:
                        raise
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
    
    # Set flag early to prevent recursion if init_db calls get_db()
    _DB_INIT_DONE = True
    try:
        _db_log("INFO", "db.init", "Starting database initialization...")
        init_db(db=db)
        _DB_INIT_LAST_ERROR = None
        _db_log("SUCCESS", "db.init", "Database initialization completed")
        return True
    except Exception as e:
        _DB_INIT_DONE = False # Reset on failure
        _DB_INIT_LAST_ERROR = repr(e)
        _db_log("ERROR", "db.init", f"Database initialization failed: {_DB_INIT_LAST_ERROR}")
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
    api_key = app.config.get("RESEND_API_KEY")
    from_email = app.config.get("MAIL_FROM")
    return bool(api_key and str(api_key).strip() and from_email and str(from_email).strip())


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


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in ("", "nan", "none", "n/a"):
        return ""
    return text


def _normalize_enrollment(value: object) -> str:
    return re.sub(r"\s+", "", _clean_text(value)).upper()


def _clean_identifier(value: object) -> str:
    import re
    text = re.sub(r"[^a-zA-Z0-9]+", "_", (str(value) if value is not None else "").strip().lower())
    return text.strip("_")


def _db_log(level: str, module: str, message: str):
    """Log database operations with consistent formatting.
    
    Args:
        level: 'INFO', 'SUCCESS', 'WARNING', 'ERROR'
        module: Module name (e.g., 'db.init', 'db.schema')
        message: Log message
    """
    level_symbols = {
        'INFO': '>>',
        'SUCCESS': 'OK',
        'WARNING': 'WN',
        'ERROR': 'ER',
    }
    symbol = level_symbols.get(level, '--')
    print(f"[{level}] [{module}] {symbol} {message}")


def _table_columns(db, table_name: str):
    """Get all column names from a table (SQLite and PostgreSQL compatible)."""
    try:
        if str(app.config.get("DATABASE", "")).startswith("postgres"):
            rows = db.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = %s AND table_schema = 'public'
                ORDER BY ordinal_position
                """,
                (table_name,),
            ).fetchall()
            return {row_get(row, "column_name") for row in rows}

        rows = db.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {row_get(row, "name") for row in rows}
    except Exception as e:
        _db_log("ERROR", "db.schema", f"Failed to get columns for table '{table_name}': {repr(e)}")
        return set()


def _ensure_column(db, table_name: str, column_name: str, column_definition: str):
    """Add a column to a table if it doesn't exist with enhanced logging.
    
    For PostgreSQL, each ALTER is wrapped in a SAVEPOINT so that if it fails
    (e.g. duplicate column from a concurrent worker), only that statement is
    rolled back and the connection is NOT left in the InFailedSqlTransaction
    state that would break all subsequent queries including login.
    """
    is_postgres = str(app.config.get("DATABASE", "")).startswith("postgres")
    
    try:
        if is_postgres:
            # Use savepoint so a failure here doesn't abort the outer transaction
            try:
                db.execute("SAVEPOINT ensure_col")
            except Exception as sp_err:
                _db_log("WARNING", "db.schema", f"Failed to create savepoint for {table_name}.{column_name}: {repr(sp_err)}")
                return
            
            try:
                columns = _table_columns(db, table_name)
                if column_name not in columns:
                    _db_log("INFO", "db.schema", f"Checking {table_name}.{column_name}")
                    db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")
                    _db_log("SUCCESS", "db.schema", f"Column added: {table_name}.{column_name} ({column_definition})")
                else:
                    _db_log("INFO", "db.schema", f"Column exists: {table_name}.{column_name}")
                db.execute("RELEASE SAVEPOINT ensure_col")
            except Exception as e:
                _db_log("ERROR", "db.schema", f"Failed to add column {table_name}.{column_name}: {repr(e)}")
                try:
                    db.execute("ROLLBACK TO SAVEPOINT ensure_col")
                    db.execute("RELEASE SAVEPOINT ensure_col")
                except Exception:
                    pass
        else:
            # SQLite
            columns = _table_columns(db, table_name)
            if column_name not in columns:
                try:
                    _db_log("INFO", "db.schema", f"Checking {table_name}.{column_name}")
                    db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")
                    _db_log("SUCCESS", "db.schema", f"Column added: {table_name}.{column_name} ({column_definition})")
                except Exception as e:
                    _db_log("ERROR", "db.schema", f"Failed to add column {table_name}.{column_name}: {repr(e)}")
            else:
                _db_log("INFO", "db.schema", f"Column exists: {table_name}.{column_name}")
    except Exception as outer_err:
        _db_log("ERROR", "db.schema", f"Unexpected error in _ensure_column for {table_name}.{column_name}: {repr(outer_err)}")


def get_teacher_context(db=None):
    """Return the logged-in teacher profile plus subject/branch/section metadata."""
    if session.get("role") != "teacher":
        return None

    created_here = False
    if db is None:
        db = get_db()
        created_here = True

    try:
        teacher_id = session.get("teacher_id")
        if not teacher_id:
            return None

        placeholder = get_placeholder()
        teacher = db.execute(
            f"SELECT * FROM teachers WHERE id = {placeholder}",
            (teacher_id,),
        ).fetchone()
        if not teacher:
            return None

        legacy_subject_id = row_get(teacher, "subject_id")
        current_subject_id = session.get("teacher_subject_id") or legacy_subject_id
        current_branch_id = session.get("teacher_branch_id")
        current_section = _normalize_branch_name(session.get("teacher_section"))

        assigned_assignments = _resolve_teacher_assignments(db, teacher_id)
        assigned_subjects = []
        assigned_branches = []
        for assignment in assigned_assignments:
            if row_get(assignment, "subject_id") is not None:
                assigned_subjects.append({
                    "id": row_get(assignment, "subject_id"),
                    "name": row_get(assignment, "subject_name"),
                    "branch_id": row_get(assignment, "branch_id"),
                })
            if row_get(assignment, "branch_id") is not None:
                assigned_branches.append({
                    "id": row_get(assignment, "branch_id"),
                    "name": row_get(assignment, "branch_name"),
                    "location": None,
                    "section": row_get(assignment, "section") or _branch_section_from_name(row_get(assignment, "branch_name")) or row_get(assignment, "branch_name"),
                })

        # Deduplicate helper lists while preserving order.
        def _dedupe_rows(rows, key_name):
            seen = set()
            unique_rows = []
            for item in rows:
                key = row_get(item, key_name)
                if key in seen:
                    continue
                seen.add(key)
                unique_rows.append(item)
            return unique_rows

        assigned_subjects = _dedupe_rows(assigned_subjects, "id")
        assigned_branches = _dedupe_rows(assigned_branches, "id")

        allowed_pairs = {
            (str(row_get(a, "subject_id")), str(row_get(a, "branch_id")), _normalize_branch_name(row_get(a, "section")))
            for a in assigned_assignments
            if row_get(a, "subject_id") is not None and row_get(a, "branch_id") is not None
        }

        if current_branch_id:
            current_branch_row = db.execute(
                f"SELECT id, name FROM branches WHERE id = {placeholder}",
                (current_branch_id,),
            ).fetchone()
            branch_label = row_get(current_branch_row, "name") if current_branch_row else ""
            current_section = current_section or _branch_section_from_name(branch_label) or branch_label

        if assigned_subjects and current_subject_id is None:
            current_subject_id = row_get(assigned_subjects[0], "id")
        if assigned_branches and current_branch_id is None:
            current_branch_id = row_get(assigned_branches[0], "id")
            current_section = row_get(assigned_branches[0], "section") or current_section or row_get(assigned_branches[0], "name")

        if current_subject_id and current_branch_id:
            pair_key = (str(current_subject_id), str(current_branch_id), _normalize_branch_name(current_section))
            if allowed_pairs and pair_key not in allowed_pairs:
                current_subject_id = None
                current_branch_id = None
                current_section = ""

        if not current_subject_id and assigned_subjects:
            current_subject_id = row_get(assigned_subjects[0], "id")
            session["teacher_subject_id"] = current_subject_id

        if not current_branch_id and assigned_branches:
            current_branch_id = row_get(assigned_branches[0], "id")
            current_section = row_get(assigned_branches[0], "section") or current_section or row_get(assigned_branches[0], "name")
            session["teacher_branch_id"] = current_branch_id
            session["teacher_section"] = current_section

        if current_subject_id is None and legacy_subject_id:
            current_subject_id = legacy_subject_id

        current_branch_name = None
        if current_branch_id:
            branch_row = db.execute(
                f"SELECT id, name, location FROM branches WHERE id = {placeholder}",
                (current_branch_id,),
            ).fetchone()
            current_branch_name = row_get(branch_row, "name") if branch_row else None

        subject = None
        if current_subject_id:
            subject = db.execute(
                f"SELECT id, name, branch_id FROM subjects WHERE id = {placeholder}",
                (current_subject_id,),
            ).fetchone()

        subject_name = row_get(subject, "name") if subject else ""

        return {
            "teacher": teacher,
            "teacher_id": row_get(teacher, "id"),
            "name": row_get(teacher, "name"),
            "username": row_get(teacher, "username"),
            "subject_name": subject_name,
            "subject_id": row_get(subject, "id") if subject else legacy_subject_id,
            "current_subject_id": row_get(subject, "id") if subject else current_subject_id,
            "subject_row": subject,
            "current_branch_id": current_branch_id,
            "current_branch_name": current_branch_name,
            "current_section": current_section,
            "assigned_branches": assigned_branches,
            "assigned_branches_count": len(assigned_branches) if assigned_branches else 0,
            "assigned_subjects": assigned_subjects,
            "assigned_subjects_count": len(assigned_subjects) if assigned_subjects else 0,
            "assigned_assignments": assigned_assignments,
            "assigned_assignments_count": len(assigned_assignments) if assigned_assignments else 0,
        }
    finally:
        if created_here:
            try:
                db.close()
            except Exception:
                pass


def send_email_resend(subject: str, recipient: str, body: str, attachments=None, html_body=None) -> Tuple[bool, str]:
    """Send email using Resend Email API.

    Returns (success: bool, error_message: str|None).
    """
    logger = logging.getLogger("app.email")
    resend_key = (app.config.get("RESEND_API_KEY") or "").strip()
    from_email = (app.config.get("MAIL_FROM") or "").strip()
    timeout = float(os.environ.get("RESEND_TIMEOUT_SECONDS", 15))

    if not resend_key or not from_email:
        err = "Email is not configured. Set RESEND_API_KEY and MAIL_FROM."
        logger.error("[email.resend] %s", err)
        return False, err

    if not is_valid_email(recipient):
        err = f"Invalid recipient email: {recipient}"
        logger.error("[email.resend] %s", err)
        return False, err

    payload = {
        "from": from_email,
        "to": [recipient],
        "subject": subject,
        "text": body,
    }
    if html_body:
        payload["html"] = html_body

    if attachments:
        encoded_attachments = []
        for attachment in attachments:
            filename = (attachment.get("filename") or "attachment").strip()
            content = attachment.get("content", b"")
            if isinstance(content, str):
                content = content.encode("utf-8")
            mimetype = attachment.get("mimetype") or "application/octet-stream"
            encoded_attachments.append({
                "filename": filename,
                "content": base64.b64encode(content).decode("ascii"),
                "type": mimetype,
            })
        payload["attachments"] = encoded_attachments

    headers = {
        "Authorization": f"Bearer {resend_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            json=payload,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as req_err:
        logger.exception("[email.resend] API request failed for recipient=%s", recipient)
        return False, f"Resend API request failed: {req_err}"

    if 200 <= response.status_code < 300:
        try:
            response_json = response.json()
        except Exception:
            response_json = {}
        message_id = response_json.get("id")
        logger.info("[email.resend] Email sent to %s (id=%s)", recipient, message_id or "n/a")
        return True, None

    response_body = (response.text or "")[:800]
    logger.error(
        "[email.resend] API error status=%s recipient=%s body=%s",
        response.status_code,
        recipient,
        response_body,
    )
    return False, f"Resend API error {response.status_code}: {response_body}"


def send_email(subject, recipient, body, attachments=None, html_body=None):
    """Compatibility wrapper for legacy `send_email` that returns bool."""
    ok, err = send_email_resend(subject, recipient, body, attachments=attachments, html_body=html_body)
    if ok:
        return True
    print(f"Email not sent: {err}")
    return False


def send_email_with_error(subject, recipient, body, attachments=None, html_body=None):
    """Compatibility wrapper that returns (success, error_message)."""
    return send_email_resend(subject, recipient, body, attachments=attachments, html_body=html_body)


def safe_send_email(subject: str, recipient: str, body: str, attachments=None, html_body=None) -> bool:
    try:
        return bool(send_email(subject, recipient, body, attachments=attachments, html_body=html_body))
    except Exception as e:
        logging.getLogger("app.email").exception("safe_send_email unexpected error")
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
            students.parent_email AS parent_email,
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
        recipients = []
        if row["email"]: recipients.append(row["email"])
        if row["parent_email"]: recipients.append(row["parent_email"])
        
        if not recipients:
            continue
            
        if row["percentage"] < threshold:
            body = (
                f"Hello {row['student_name']} and Parent/Guardian,\n\n"
                f"The current attendance for {row['student_name']} is {row['percentage']}%, which is below the minimum required threshold of {threshold}%.\n"
                "Please attend classes regularly and check the attendance dashboard for details.\n\n"
                "If you have any questions, contact your instructor.\n\n"
                "Best regards,\n"
                "Attendance Management Team"
            )
            html_body = (
                f"<div style='font-family:Arial,sans-serif;line-height:1.6;color:#1f2937'>"
                f"<h2 style='margin-bottom:8px;color:#ef476f'>Low Attendance Alert</h2>"
                f"<p>Hello <strong>{row['student_name']} and Parent/Guardian</strong>,</p>"
                f"<p>The current attendance for <strong>{row['student_name']}</strong> is <strong>{row['percentage']}%</strong>, which is below the required threshold of <strong>{threshold}%</strong>.</p>"
                "<p>Please attend classes regularly and check your dashboard for details.</p>"
                "<p style='margin-top:16px'>Best regards,<br>Attendance Management Team</p>"
                "</div>"
            )
            
            # Send to all recipients
            for rec in recipients:
                if send_email(
                    subject=f"Low Attendance Alert: {row['percentage']}%",
                    recipient=rec,
                    body=body,
                    html_body=html_body,
                ):
                    emailed_students.append({
                        "name": row["student_name"],
                        "email": rec,
                        "percentage": row["percentage"],
                    })

    return emailed_students


def _send_low_attendance_background(student_ids):
    """Background task so attendance save response is never blocked by email API calls."""
    db = None
    try:
        db = get_db()
        emailed = notify_low_attendance(db, student_ids)
        print(f"[mark_attendance] Low-attendance email task complete. emailed={len(emailed)}")
    except Exception as e:
        print(f"[mark_attendance] Low-attendance email task failed: {repr(e)}")
        print(traceback.format_exc())
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


def dispatch_low_attendance_notifications(student_ids):
    if not student_ids or not is_mail_configured():
        return
    try:
        # Preferred path for eventlet/gevent compatible servers.
        socketio.start_background_task(_send_low_attendance_background, list(student_ids))
    except Exception as e:
        print(f"[mark_attendance] socketio background task unavailable, using thread fallback: {repr(e)}")
        t = threading.Thread(target=_send_low_attendance_background, args=(list(student_ids),), daemon=True)
        t.start()


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
        CREATE TABLE IF NOT EXISTS teachers (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            subject_id INTEGER,
            branch_id INTEGER NOT NULL
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
        CREATE TABLE IF NOT EXISTS students (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            enrollment TEXT UNIQUE NOT NULL,
            roll_no TEXT,
            branch_id INTEGER NOT NULL,
            section TEXT,
            email TEXT,
            parent_email TEXT,
            current_year INTEGER DEFAULT 1,
            current_semester INTEGER DEFAULT 1,
            import_order INTEGER
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
            branch_section TEXT,
            section TEXT,
            subject_id INTEGER NOT NULL,
            teacher_id INTEGER,
            subject_name TEXT,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT,
            period INTEGER DEFAULT 1
        );
        """)

        db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id SERIAL PRIMARY KEY,
            key TEXT UNIQUE NOT NULL,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS teacher_assignments (
            id SERIAL PRIMARY KEY,
            teacher_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            branch_id INTEGER NOT NULL,
            section TEXT,
            UNIQUE(teacher_id, subject_id, branch_id, section),
            FOREIGN KEY(teacher_id) REFERENCES teachers(id) ON DELETE CASCADE,
            FOREIGN KEY(subject_id) REFERENCES subjects(id) ON DELETE CASCADE,
            FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE CASCADE
        );
        """)

        # Index creation moved to upgrade section to ensure columns exist first.
    else:
        # SQLite
        db.executescript("""
        CREATE TABLE IF NOT EXISTS teachers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            subject_id INTEGER,
            branch_id INTEGER NOT NULL,
            FOREIGN KEY(subject_id) REFERENCES subjects(id),
            FOREIGN KEY(branch_id) REFERENCES branches(id)
        );

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
            roll_no TEXT,
            branch_id INTEGER NOT NULL,
            section TEXT,
            email TEXT,
            parent_email TEXT,
            current_year INTEGER DEFAULT 1,
            current_semester INTEGER DEFAULT 1,
            import_order INTEGER
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY,
            student_id INTEGER NOT NULL,
            branch_id INTEGER NOT NULL,
            branch_section TEXT,
            section TEXT,
            subject_id INTEGER NOT NULL,
            teacher_id INTEGER,
            subject_name TEXT,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT,
            period INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            key TEXT UNIQUE NOT NULL,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS teacher_assignments (
            id INTEGER PRIMARY KEY,
            teacher_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            branch_id INTEGER NOT NULL,
            section TEXT,
            UNIQUE(teacher_id, subject_id, branch_id, section),
            FOREIGN KEY(teacher_id) REFERENCES teachers(id) ON DELETE CASCADE,
            FOREIGN KEY(subject_id) REFERENCES subjects(id) ON DELETE CASCADE,
            FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE CASCADE
        );

        -- Index creation moved to upgrade section to ensure columns exist first.
        """)

    # Best-effort schema upgrades for existing databases.
    # IMPORTANT: For PostgreSQL, we rollback any stale aborted transaction
    # that may have been left by a previous failed connection before doing
    # any DDL work. This prevents InFailedSqlTransaction from poisoning the
    # connection and breaking the login route.
    is_postgres = str(app.config.get("DATABASE", "")).startswith("postgres")
    if is_postgres:
        try:
            db.rollback()
        except Exception:
            pass

    try:
        _ensure_column(db, "attendance", "teacher_id", "INTEGER")
        _ensure_column(db, "attendance", "subject_name", "TEXT")
        _ensure_column(db, "attendance", "period", "INTEGER DEFAULT 1")
        _ensure_column(db, "attendance", "branch_section", "TEXT")
        _ensure_column(db, "attendance", "section", "TEXT")
        _ensure_column(db, "students", "import_order", "INTEGER")
        _ensure_column(db, "students", "parent_email", "TEXT")
        _ensure_column(db, "students", "current_year", "INTEGER DEFAULT 1")
        _ensure_column(db, "students", "current_semester", "INTEGER DEFAULT 1")
        _ensure_column(db, "students", "roll_no", "TEXT")
        _ensure_column(db, "students", "section", "TEXT")
        _ensure_column(db, "users", "student_id", "INTEGER")
        _ensure_column(db, "branches", "location", "TEXT")
        _ensure_column(db, "teachers", "name", "TEXT NOT NULL")
        _ensure_column(db, "teachers", "subject_id", "INTEGER")
        _ensure_column(db, "teachers", "subject_name", "TEXT")

        try:
            db.execute(
                """
                UPDATE attendance
                SET branch_section = COALESCE(
                    branch_section,
                    (SELECT name FROM branches WHERE branches.id = attendance.branch_id)
                )
                WHERE branch_section IS NULL OR TRIM(branch_section) = ''
                """
            )
        except Exception:
            pass

        try:
            db.execute(
                """
                UPDATE students
                SET roll_no = COALESCE(roll_no, enrollment),
                    section = COALESCE(section, '')
                WHERE roll_no IS NULL OR section IS NULL OR TRIM(section) = ''
                """
            )
        except Exception as e:
            print(f"[DB] UPDATE students for roll_no/section failed: {repr(e)}")
            pass

        try:
            db.execute(
                """
                UPDATE attendance
                SET section = COALESCE(section, branch_section)
                WHERE section IS NULL OR TRIM(section) = ''
                """
            )
        except Exception:
            pass
        
        # Drop the NOT NULL constraint on teachers.subject_name so new inserts
        # that use subject_id instead of subject_name don't violate the constraint.
        if is_postgres:
            try:
                db.execute("SAVEPOINT drop_subject_name_notnull")
                db.execute("ALTER TABLE teachers ALTER COLUMN subject_name DROP NOT NULL")
                db.execute("RELEASE SAVEPOINT drop_subject_name_notnull")
                db.commit()
            except Exception as e:
                print(f"[DB] subject_name NOT NULL drop skipped: {repr(e)}")
                try:
                    db.execute("ROLLBACK TO SAVEPOINT drop_subject_name_notnull")
                    db.execute("RELEASE SAVEPOINT drop_subject_name_notnull")
                except Exception: pass
        
        db.commit() # Commit column additions before index creation
        
        # Verify and ensure critical columns exist
        _db_log("INFO", "db.init", "Starting critical column verification...")
        critical_columns = [
            ("students", "roll_no", "TEXT", "Student roll number"),
            ("students", "section", "TEXT", "Student section/class"),
            ("students", "import_order", "INTEGER", "Import order for data consistency"),
            ("attendance", "subject_id", "INTEGER", "Subject ID foreign key"),
            ("attendance", "period", "INTEGER DEFAULT 1", "Period number for multiple attendance periods"),
        ]
        
        columns_verified = 0
        columns_added = 0
        columns_failed = 0
        
        for table, col, col_type, description in critical_columns:
            try:
                _db_log("INFO", "db.verify", f"Checking {table}.{col} - {description}")
                columns = _table_columns(db, table)
                if col not in columns:
                    _db_log("WARNING", "db.verify", f"Missing column: {table}.{col}")
                    try:
                        if is_postgres:
                            db.execute("SAVEPOINT verify_col")
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                        if is_postgres:
                            db.execute("RELEASE SAVEPOINT verify_col")
                        db.commit()
                        _db_log("SUCCESS", "db.verify", f"Column added: {table}.{col}")
                        columns_added += 1
                    except Exception as add_err:
                        _db_log("ERROR", "db.verify", f"Failed to add {table}.{col}: {repr(add_err)}")
                        if is_postgres:
                            try:
                                db.execute("ROLLBACK TO SAVEPOINT verify_col")
                                db.execute("RELEASE SAVEPOINT verify_col")
                            except Exception:
                                pass
                        columns_failed += 1
                        if hasattr(db, 'rollback'):
                            db.rollback()
                else:
                    _db_log("SUCCESS", "db.verify", f"Column exists: {table}.{col}")
                    columns_verified += 1
            except Exception as verify_err:
                _db_log("ERROR", "db.verify", f"Verification failed for {table}.{col}: {repr(verify_err)}")
                columns_failed += 1
        
        # Summary of verification
        _db_log("INFO", "db.init", f"Critical columns verification summary:")
        _db_log("INFO", "db.init", f"  [OK] Verified: {columns_verified}")
        _db_log("INFO", "db.init", f"  + Added: {columns_added}")
        if columns_failed > 0:
            _db_log("WARNING", "db.init", f"  [ER] Failed: {columns_failed}")
        else:
            _db_log("SUCCESS", "db.init", f"  [ER] Failed: 0")
        
        # Upgrade index if it doesn't support period
        _db_log("INFO", "db.init", "Upgrading database indexes...")
        try:
            if is_postgres:
                db.execute("SAVEPOINT idx_upgrade")
            db.execute("DROP INDEX IF EXISTS idx_attendance_student_subject_date")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_student_subject_date_period ON attendance(student_id, subject_id, date, period)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance(date)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_attendance_teacher ON attendance(teacher_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_attendance_subject ON attendance(subject_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_attendance_student ON attendance(student_id)")
            if is_postgres:
                db.execute("RELEASE SAVEPOINT idx_upgrade")
            db.commit()
            _db_log("SUCCESS", "db.init", "Database indexes upgraded successfully")
        except Exception as idx_error:
            _db_log("WARNING", "db.init", f"Index upgrade skipped: {repr(idx_error)}")
            if is_postgres:
                try:
                    db.execute("ROLLBACK TO SAVEPOINT idx_upgrade")
                    db.execute("RELEASE SAVEPOINT idx_upgrade")
                except Exception: pass
            if hasattr(db, 'rollback'): db.rollback()
        
        try:
            db.execute("CREATE INDEX IF NOT EXISTS idx_students_import_order ON students(import_order, id)")
            db.commit()
            _db_log("SUCCESS", "db.init", "Index created: idx_students_import_order")
        except Exception as idx_err:
            _db_log("WARNING", "db.init", f"Failed to create idx_students_import_order: {repr(idx_err)}")
            if hasattr(db, 'rollback'): db.rollback()

        # Backfill import_order once for legacy rows (keeps existing relative order by id).
        _db_log("INFO", "db.init", "Processing import order for legacy students...")
        try:
            max_row = db.execute("SELECT COALESCE(MAX(import_order), 0) AS max_import_order FROM students").fetchone()
            next_import_order = int(row_get(max_row, "max_import_order", 0) or 0) + 1
            missing_order_rows = db.execute("SELECT id FROM students WHERE import_order IS NULL ORDER BY id").fetchall()
            if missing_order_rows:
                for row in missing_order_rows:
                    student_id = row_get(row, "id")
                    if student_id is None:
                        continue
                    db.execute(
                        f"UPDATE students SET import_order = {placeholder} WHERE id = {placeholder}",
                        (next_import_order, student_id),
                    )
                    next_import_order += 1
                _db_log("SUCCESS", "db.init", f"Updated import_order for {len(missing_order_rows)} students")
            db.commit()
        except Exception as import_err:
            _db_log("WARNING", "db.init", f"Import order update skipped: {repr(import_err)}")
            if hasattr(db, 'rollback'):
                db.rollback()

        if str(app.config.get("DATABASE", "")).startswith("postgres"):
            db.execute("""
                CREATE TABLE IF NOT EXISTS teachers (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    subject_id INTEGER,
                    branch_id INTEGER NOT NULL,
                    subject_name TEXT
                )
            """)
        else:
            db.execute("""
                CREATE TABLE IF NOT EXISTS teachers (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    subject_id INTEGER,
                    branch_id INTEGER NOT NULL,
                    subject_name TEXT
                )
            """)
    except Exception as upgrade_error:
        _db_log("WARNING", "db.init", f"Teacher schema upgrade skipped: {repr(upgrade_error)}")

    # ✅ Create teacher_branches junction table for multi-branch support
    try:
        if is_postgres:
            db.execute("""
                CREATE TABLE IF NOT EXISTS teacher_branches (
                    id SERIAL PRIMARY KEY,
                    teacher_id INTEGER NOT NULL,
                    branch_id INTEGER NOT NULL,
                    UNIQUE(teacher_id, branch_id),
                    FOREIGN KEY(teacher_id) REFERENCES teachers(id) ON DELETE CASCADE,
                    FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE CASCADE
                )
            """)
        else:
            db.execute("""
                CREATE TABLE IF NOT EXISTS teacher_branches (
                    id INTEGER PRIMARY KEY,
                    teacher_id INTEGER NOT NULL,
                    branch_id INTEGER NOT NULL,
                    UNIQUE(teacher_id, branch_id),
                    FOREIGN KEY(teacher_id) REFERENCES teachers(id) ON DELETE CASCADE,
                    FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE CASCADE
                )
            """)
        
        # Migrate existing teacher-branch assignments from teachers.branch_id
        # One-time migration: for each teacher with a branch_id, insert into teacher_branches if not already there
        try:
            existing_assignments = db.execute("SELECT COUNT(*) as count FROM teacher_branches").fetchone()
            count = row_get(existing_assignments, "count", 0)
            if count == 0:
                # First migration - populate from teachers.branch_id
                teachers_with_branches = db.execute("SELECT DISTINCT id, branch_id FROM teachers WHERE branch_id IS NOT NULL").fetchall()
                for teacher_row in teachers_with_branches:
                    t_id = row_get(teacher_row, "id")
                    b_id = row_get(teacher_row, "branch_id")
                    if t_id and b_id:
                        try:
                            db.execute(
                                f"INSERT INTO teacher_branches (teacher_id, branch_id) VALUES ({placeholder}, {placeholder})",
                                (t_id, b_id)
                            )
                        except Exception:
                            pass  # Duplicate or other constraint - ignore
                db.commit()
                print("[DB] Migrated teacher-branch assignments to new junction table")
        except Exception as migration_error:
            print(f"[DB] teacher_branches migration skipped: {repr(migration_error)}")
            if hasattr(db, 'rollback'): db.rollback()
        
        db.commit()
    except Exception as tb_error:
        print(f"[DB] teacher_branches table creation skipped: {repr(tb_error)}")

    # ✅ Create teacher_subjects junction table for multi-subject support
    try:
        if is_postgres:
            db.execute("""
                CREATE TABLE IF NOT EXISTS teacher_subjects (
                    id SERIAL PRIMARY KEY,
                    teacher_id INTEGER NOT NULL,
                    subject_id INTEGER NOT NULL,
                    UNIQUE(teacher_id, subject_id),
                    FOREIGN KEY(teacher_id) REFERENCES teachers(id) ON DELETE CASCADE,
                    FOREIGN KEY(subject_id) REFERENCES subjects(id) ON DELETE CASCADE
                )
            """)
        else:
            db.execute("""
                CREATE TABLE IF NOT EXISTS teacher_subjects (
                    id INTEGER PRIMARY KEY,
                    teacher_id INTEGER NOT NULL,
                    subject_id INTEGER NOT NULL,
                    UNIQUE(teacher_id, subject_id),
                    FOREIGN KEY(teacher_id) REFERENCES teachers(id) ON DELETE CASCADE,
                    FOREIGN KEY(subject_id) REFERENCES subjects(id) ON DELETE CASCADE
                )
            """)

        # One-time migration from legacy teachers.subject_id
        _db_log("INFO", "db.init", "Checking for teacher-subject junction table migration...")
        try:
            existing_assignments = db.execute("SELECT COUNT(*) as count FROM teacher_subjects").fetchone()
            count = row_get(existing_assignments, "count", 0)
            if count == 0:
                _db_log("INFO", "db.init", "Migrating legacy teacher-subject assignments...")
                teachers_with_subjects = db.execute(
                    "SELECT DISTINCT id, subject_id FROM teachers WHERE subject_id IS NOT NULL"
                ).fetchall()
                migrated_count = 0
                for teacher_row in teachers_with_subjects:
                    t_id = row_get(teacher_row, "id")
                    s_id = row_get(teacher_row, "subject_id")
                    if t_id and s_id:
                        try:
                            db.execute(
                                f"INSERT INTO teacher_subjects (teacher_id, subject_id) VALUES ({placeholder}, {placeholder})",
                                (t_id, s_id),
                            )
                            migrated_count += 1
                        except Exception:
                            pass
                db.commit()
                _db_log("SUCCESS", "db.init", f"Migrated {migrated_count} teacher-subject assignments to junction table")
            else:
                _db_log("INFO", "db.init", f"Teacher-subject migration already complete ({count} records)")
        except Exception as migration_error:
            _db_log("WARNING", "db.init", f"Teacher-subject migration skipped: {repr(migration_error)}")
            if hasattr(db, 'rollback'):
                db.rollback()

        db.commit()
    except Exception as ts_error:
        _db_log("WARNING", "db.init", f"Teacher-subjects table creation skipped: {repr(ts_error)}")

    try:
        if is_postgres:
            db.execute("""
                CREATE TABLE IF NOT EXISTS teacher_assignments (
                    id SERIAL PRIMARY KEY,
                    teacher_id INTEGER NOT NULL,
                    subject_id INTEGER NOT NULL,
                    branch_id INTEGER NOT NULL,
                    section TEXT,
                    UNIQUE(teacher_id, subject_id, branch_id, section),
                    FOREIGN KEY(teacher_id) REFERENCES teachers(id) ON DELETE CASCADE,
                    FOREIGN KEY(subject_id) REFERENCES subjects(id) ON DELETE CASCADE,
                    FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE CASCADE
                )
            """)
        else:
            db.execute("""
                CREATE TABLE IF NOT EXISTS teacher_assignments (
                    id INTEGER PRIMARY KEY,
                    teacher_id INTEGER NOT NULL,
                    subject_id INTEGER NOT NULL,
                    branch_id INTEGER NOT NULL,
                    section TEXT,
                    UNIQUE(teacher_id, subject_id, branch_id, section),
                    FOREIGN KEY(teacher_id) REFERENCES teachers(id) ON DELETE CASCADE,
                    FOREIGN KEY(subject_id) REFERENCES subjects(id) ON DELETE CASCADE,
                    FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE CASCADE
                )
            """)

        existing_assignments = db.execute("SELECT COUNT(*) as count FROM teacher_assignments").fetchone()
        if row_get(existing_assignments, "count", 0) == 0:
            teachers = db.execute("SELECT DISTINCT id FROM teachers").fetchall()
            for teacher_row in teachers:
                teacher_id = row_get(teacher_row, "id")
                if teacher_id is None:
                    continue
                branch_rows = db.execute(
                    f"SELECT b.id, b.name FROM branches b JOIN teacher_branches tb ON tb.branch_id = b.id WHERE tb.teacher_id = {placeholder}",
                    (teacher_id,),
                ).fetchall()
                subject_rows = db.execute(
                    f"SELECT s.id FROM subjects s JOIN teacher_subjects ts ON ts.subject_id = s.id WHERE ts.teacher_id = {placeholder}",
                    (teacher_id,),
                ).fetchall()
                for branch_row in branch_rows:
                    branch_id = row_get(branch_row, "id")
                    branch_name = row_get(branch_row, "name") or ""
                    section = _branch_section_from_name(branch_name)
                    for subject_row in subject_rows:
                        subject_id = row_get(subject_row, "id")
                        if branch_id is None or subject_id is None:
                            continue
                        try:
                            db.execute(
                                f"INSERT INTO teacher_assignments (teacher_id, subject_id, branch_id, section) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
                                (teacher_id, subject_id, branch_id, section),
                            )
                        except Exception:
                            pass
            db.commit()
        else:
            _db_log("INFO", "db.init", f"Teacher-assignments already complete ({row_get(existing_assignments, 'count', 0)} records)")
    except Exception as ta_error:
        _db_log("WARNING", "db.init", f"Teacher-assignments creation skipped: {repr(ta_error)}")

    # ✅ Admin check
    _db_log("INFO", "db.init", "Verifying admin user...")
    admin = db.execute(
        f"SELECT id FROM users WHERE username = {placeholder}", ("admin",)
    ).fetchone()

    if not admin:
        _db_log("WARNING", "db.init", "Admin user not found, creating default admin account...")
        try:
            db.execute(
                f"INSERT INTO users (username, password, role) VALUES ({placeholder}, {placeholder}, {placeholder})",
                ("admin", generate_password_hash("admin123"), "admin"),
            )
            db.commit()
            _db_log("SUCCESS", "db.init", "Default admin account created (username: admin)")
        except Exception as admin_err:
            _db_log("ERROR", "db.init", f"Failed to create admin account: {repr(admin_err)}")
    else:
        _db_log("SUCCESS", "db.init", "Admin user verified")

    # No default teacher accounts are seeded.
    # Teachers must be created manually by an admin via /admin/teachers.

    # ✅ Ensure at least one branch exists
    _db_log("INFO", "db.init", "Verifying default branch...")
    default_branch = db.execute(f"SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()
    if not default_branch:
        _db_log("WARNING", "db.init", "No branch found, creating default branch...")
        try:
            db.execute(f"INSERT INTO branches (name, location) VALUES ({placeholder}, {placeholder})", ("General Branch", "Main Campus"))
            db.commit()
            default_branch = db.execute(f"SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()
            _db_log("SUCCESS", "db.init", "Default branch created")
        except Exception as branch_err:
            _db_log("ERROR", "db.init", f"Failed to create default branch: {repr(branch_err)}")
    else:
        _db_log("SUCCESS", "db.init", "Default branch verified")
    
    default_branch_id = row_get(default_branch, "id")

    # ✅ Create normalized timetable_entries table for production-ready timetable data
    try:
        if str(app.config.get("DATABASE", "")).startswith("postgres"):
            db.execute("""
            CREATE TABLE IF NOT EXISTS timetable_entries (
                id SERIAL PRIMARY KEY,
                branch_id INTEGER,
                section TEXT,
                semester INTEGER,
                day TEXT,
                start_time TEXT,
                end_time TEXT,
                subject_id INTEGER,
                teacher_id INTEGER,
                is_lab INTEGER DEFAULT 0,
                room TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
        else:
            db.execute("""
            CREATE TABLE IF NOT EXISTS timetable_entries (
                id INTEGER PRIMARY KEY,
                branch_id INTEGER,
                section TEXT,
                semester INTEGER,
                day TEXT,
                start_time TEXT,
                end_time TEXT,
                subject_id INTEGER,
                teacher_id INTEGER,
                is_lab INTEGER DEFAULT 0,
                room TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
        db.commit()
        _db_log("SUCCESS", "db.init", "timetable_entries table ensured")
    except Exception as te_err:
        _db_log("WARNING", "db.init", f"timetable_entries creation skipped: {repr(te_err)}")

    # Safe migration: map legacy timetable_slots -> timetable_entries (best-effort)
    try:
        # Only migrate if timetable_slots exist and timetable_entries is empty
        cols = _table_columns(db, "timetable_slots")
        if cols:
            existing = db.execute("SELECT COUNT(1) AS c FROM timetable_entries").fetchone()
            existing_count = int(row_get(existing, "c", 0) or 0)
            if existing_count == 0:
                _db_log("INFO", "db.migrate", "Starting migration from timetable_slots to timetable_entries")
                slots = db.execute("SELECT id, branch, section, semester, day, start_time, end_time, subject_name, faculty_name, is_lab, room FROM timetable_slots ORDER BY id").fetchall()
                migrated = 0
                placeholder = get_placeholder()
                for s in slots:
                    bname = row_get(s, "branch") or ""
                    sec = row_get(s, "section") or ""
                    sem = row_get(s, "semester")
                    day = row_get(s, "day") or ""
                    st = row_get(s, "start_time") or ""
                    et = row_get(s, "end_time") or ""
                    subj_name = row_get(s, "subject_name") or ""
                    fac_name = row_get(s, "faculty_name") or ""
                    is_lab = int(row_get(s, "is_lab") or 0)
                    room = row_get(s, "room") or ""

                    branch_id = None
                    try:
                        row = db.execute(f"SELECT id FROM branches WHERE LOWER(name)=LOWER({placeholder}) LIMIT 1", (bname,)).fetchone()
                        branch_id = row_get(row, "id") if row else None
                    except Exception:
                        branch_id = None

                    subject_id = None
                    try:
                        row = db.execute(f"SELECT id FROM subjects WHERE LOWER(name)=LOWER({placeholder}) LIMIT 1", (subj_name,)).fetchone()
                        subject_id = row_get(row, "id") if row else None
                    except Exception:
                        subject_id = None

                    teacher_id = None
                    try:
                        row = db.execute(f"SELECT id FROM teachers WHERE LOWER(name)=LOWER({placeholder}) LIMIT 1", (fac_name,)).fetchone()
                        teacher_id = row_get(row, "id") if row else None
                    except Exception:
                        teacher_id = None

                    try:
                        db.execute(
                            f"INSERT INTO timetable_entries (branch_id, section, semester, day, start_time, end_time, subject_id, teacher_id, is_lab, room) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
                            (branch_id, sec, sem, day, st, et, subject_id, teacher_id, is_lab, room),
                        )
                        migrated += 1
                    except Exception:
                        pass
                try:
                    db.commit()
                except Exception:
                    pass
                _db_log("SUCCESS", "db.migrate", f"Migrated {migrated} timetable_slots rows to timetable_entries (best-effort)")
    except Exception as mig_err:
        _db_log("WARNING", "db.migrate", f"Timetable migration skipped: {repr(mig_err)}")
    # ✅ Ensure sample subjects exist
    _db_log("INFO", "db.init", "Verifying sample subjects...")
    sample_subjects = ["Mathematics", "Physics", "Chemistry", "English", "Programming", "Data Structures"]
    subjects_created = 0
    for sub_name in sample_subjects:
        try:
            existing_sub = db.execute(f"SELECT id FROM subjects WHERE name = {placeholder}", (sub_name,)).fetchone()
            if not existing_sub:
                db.execute(f"INSERT INTO subjects (name, branch_id) VALUES ({placeholder}, {placeholder})", (sub_name, default_branch_id))
                subjects_created += 1
        except Exception as subject_err:
            _db_log("WARNING", "db.init", f"Failed to create subject '{sub_name}': {repr(subject_err)}")
    
    if subjects_created > 0:
        db.commit()
        _db_log("SUCCESS", "db.init", f"Created {subjects_created} sample subjects")
    else:
        _db_log("SUCCESS", "db.init", "Sample subjects already exist")
    # No demo teacher accounts are seeded automatically.
    # All teacher accounts must be created by an admin via the Manage Teachers page.

    # ✅ Default low attendance threshold setting
    _db_log("INFO", "db.init", "Verifying low attendance threshold setting...")
    try:
        if not db.execute(f"SELECT id FROM settings WHERE key = {placeholder}", ("low_attendance_threshold",)).fetchone():
            _db_log("WARNING", "db.init", "Threshold setting not found, creating default...")
            db.execute(
                f"INSERT INTO settings (key, value) VALUES ({placeholder}, {placeholder})",
                ("low_attendance_threshold", str(app.config["LOW_ATTENDANCE_THRESHOLD"])),
            )
            db.commit()
            _db_log("SUCCESS", "db.init", f"Threshold setting created (threshold: {app.config['LOW_ATTENDANCE_THRESHOLD']}%)")
        else:
            _db_log("SUCCESS", "db.init", "Threshold setting verified")
    except Exception as settings_err:
        _db_log("ERROR", "db.init", f"Failed to verify threshold setting: {repr(settings_err)}")

    db.commit()
    _db_log("SUCCESS", "db.init", "Database initialization completed successfully")
    
    if created_here:
        try:
            db.close()
        except Exception:
            pass


def _normalize_branch_name(name):
    return (name or "").strip()


def _branch_section_from_name(branch_name):
    branch_name = _normalize_branch_name(branch_name)
    if "-" not in branch_name:
        return ""
    return branch_name.rsplit("-", 1)[-1].strip()


def _branch_base_from_name(branch_name):
    branch_name = _normalize_branch_name(branch_name)
    if "-" not in branch_name:
        return branch_name
    return branch_name.rsplit("-", 1)[0].strip()


def _branch_name_exists(db, branch_name, exclude_id=None):
    placeholder = get_placeholder()
    branch_name = _normalize_branch_name(branch_name)
    if not branch_name:
        return False

    if exclude_id:
        row = db.execute(
            f"SELECT 1 FROM branches WHERE LOWER(name) = LOWER({placeholder}) AND id != {placeholder}",
            (branch_name, exclude_id),
        ).fetchone()
    else:
        row = db.execute(
            f"SELECT 1 FROM branches WHERE LOWER(name) = LOWER({placeholder})",
            (branch_name,),
        ).fetchone()
    return bool(row)


def _build_branch_section_names(base_name, sections_value):
    base_name = _normalize_branch_name(base_name)
    sections_value = _normalize_branch_name(sections_value)

    if not sections_value:
        return [base_name] if base_name else []

    branch_names = []
    for section in sections_value.split(","):
        section = _normalize_branch_name(section)
        if not section:
            continue
        branch_name = f"{base_name}-{section}" if base_name else section
        if branch_name not in branch_names:
            branch_names.append(branch_name)
    return branch_names


def _get_branch_section_name(db, branch_id):
    if not branch_id:
        return None
    placeholder = get_placeholder()
    row = db.execute(
        f"SELECT name FROM branches WHERE id = {placeholder}",
        (branch_id,),
    ).fetchone()
    return row_get(row, "name") if row else None


def _get_branch_name_and_section(db, branch_id):
    if not branch_id:
        return None, None
    placeholder = get_placeholder()
    row = db.execute(
        f"SELECT id, name FROM branches WHERE id = {placeholder}",
        (branch_id,),
    ).fetchone()
    branch_name = row_get(row, "name") if row else None
    return branch_name, _branch_section_from_name(branch_name) if branch_name else None


def _resolve_teacher_assignments(db, teacher_id):
    placeholder = get_placeholder()
    assignments = db.execute(
        f"""
        SELECT ta.id, ta.teacher_id, ta.subject_id, ta.branch_id, ta.section,
               s.name AS subject_name, b.name AS branch_name
        FROM teacher_assignments ta
        JOIN subjects s ON ta.subject_id = s.id
        JOIN branches b ON ta.branch_id = b.id
        WHERE ta.teacher_id = {placeholder}
        ORDER BY s.name, b.name, ta.section
        """,
        (teacher_id,),
    ).fetchall()

    if assignments:
        return assignments

    # Backward-compatible fallback from the legacy junction tables.
    branch_rows = db.execute(
        f"""
        SELECT b.id, b.name, b.location
        FROM branches b
        JOIN teacher_branches tb ON b.id = tb.branch_id
        WHERE tb.teacher_id = {placeholder}
        ORDER BY b.name
        """,
        (teacher_id,),
    ).fetchall()
    subject_rows = db.execute(
        f"""
        SELECT s.id, s.name, s.branch_id
        FROM subjects s
        JOIN teacher_subjects ts ON s.id = ts.subject_id
        WHERE ts.teacher_id = {placeholder}
        ORDER BY s.name
        """,
        (teacher_id,),
    ).fetchall()

    fallback_assignments = []
    for subject in subject_rows:
        for branch in branch_rows:
            branch_name = row_get(branch, "name")
            fallback_assignments.append({
                "id": f"legacy-{teacher_id}-{row_get(subject, 'id')}-{row_get(branch, 'id')}",
                "teacher_id": teacher_id,
                "subject_id": row_get(subject, "id"),
                "branch_id": row_get(branch, "id"),
                "section": _branch_section_from_name(branch_name),
                "subject_name": row_get(subject, "name"),
                "branch_name": branch_name,
            })
    return fallback_assignments


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login first.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


def teacher_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login first.", "warning")
            return redirect(url_for("teacher_login", next=request.path))
        if session.get("role") != "teacher":
            return "Unauthorized Access", 403
        return f(*args, **kwargs)

    return decorated_function


def student_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login first.", "warning")
            return redirect(url_for("student_login", next=request.path))
        if session.get("role") != "student":
            return "Unauthorized Access", 403
        return f(*args, **kwargs)

    return decorated_function


def student_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("role") != "student":
            return "Unauthorized Access", 403
        if not session.get("student_id"):
            session.clear()
            flash("Please login first.", "warning")
            return redirect(url_for("student_login"))
        return f(*args, **kwargs)

    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # If not logged in, send to admin login with the required message.
        if "user_id" not in session:
            flash("Please login first.", "warning")
            return redirect(url_for("login"))
        # If logged in but not an admin, force re-authentication via admin login.
        if session.get("role") != "admin":
            flash("Please login first.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated_function


def teacher_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("role") != "teacher":
            return "Unauthorized Access", 403
        teacher_id = session.get("teacher_id")
        if not teacher_id:
            return "Unauthorized Access", 403
        if str(session.get("user_id")) != str(teacher_id):
            session.clear()
            return "Unauthorized Access", 403
        return f(*args, **kwargs)

    return decorated_function


@app.before_request
def _validate_role_session():
    """Lightweight session validation to improve stability.

    - Validates session structure on every request.
    - Periodically validates that the referenced user/teacher still exists.
    """
    try:
        if request.path.startswith("/static/"):
            return

        role = session.get("role")
        if not role:
            return

        if role not in ("admin", "teacher", "student"):
            session.clear()
            return

        if "user_id" not in session:
            session.clear()
            return

        if role == "teacher":
            if not session.get("teacher_id"):
                session.clear()
                return
            if str(session.get("user_id")) != str(session.get("teacher_id")):
                session.clear()
                return

        # DB validation at most once per 5 minutes
        now = int(time.time())
        last = int(session.get("_validated_at") or 0)
        if now - last < 300:
            return

        db = None
        try:
            db = get_db()
            placeholder = get_placeholder()

            if role in ("admin", "student"):
                row = db.execute(
                    f"SELECT role FROM users WHERE id = {placeholder}",
                    (session.get("user_id"),),
                ).fetchone()
                if not row or row_get(row, "role") != role:
                    session.clear()
                    return

            if role == "teacher":
                row = db.execute(
                    f"SELECT id FROM teachers WHERE id = {placeholder}",
                    (session.get("teacher_id"),),
                ).fetchone()
                if not row:
                    session.clear()
                    return
        except Exception:
            # Do not block requests if DB is down; routes will handle errors.
            return
        finally:
            try:
                if db is not None:
                    db.close()
            except Exception:
                pass

        session["_validated_at"] = now
    except Exception:
        return


@app.after_request
def _add_no_cache_headers(response):
    try:
        if request.path.startswith("/static/"):
            return response
        if session.get("user_id"):
            # Prevent cached authenticated pages showing after logout
            response.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            response.headers.setdefault("Pragma", "no-cache")
            response.headers.setdefault("Expires", "0")
    except Exception:
        pass
    return response


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
            # Safety: clear any stale aborted transaction (PostgreSQL only).
            # If init_db left the connection in a failed state, this resets it.
            if str(app.config.get("DATABASE", "")).startswith("postgres"):
                try:
                    db.rollback()
                except Exception:
                    pass
            placeholder = get_placeholder()
            user = db.execute(
                f"SELECT id, username, password, role FROM users WHERE username = {placeholder}",
                (username,),
            ).fetchone()

            password_ok = False
            if user:
                stored = row_get(user, "password") or ""
                try:
                    password_ok = check_password_hash(stored, password)
                except Exception:
                    # Legacy plaintext password detected; upgrade on successful match
                    password_ok = (stored == password)
                    if password_ok:
                        try:
                            db.execute(
                                f"UPDATE users SET password = {placeholder} WHERE id = {placeholder}",
                                (generate_password_hash(password), row_get(user, "id")),
                            )
                            db.commit()
                        except Exception:
                            try:
                                db.rollback()
                            except Exception:
                                pass

            if user and password_ok:
                if row_get(user, "role") == "student":
                    flash("Please use the student login page.", "error")
                    return redirect(url_for("student_login"))

                session.clear()
                session["user_id"] = row_get(user, "id")
                session["username"] = row_get(user, "username")
                session["role"] = row_get(user, "role")
                session.permanent = True
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

    return render_template("login.html", hide_nav=True)


@app.route("/logout")
@login_required
def logout():
    role = session.get("role")
    session.clear()
    dest = "login"
    if role == "teacher":
        dest = "teacher_login"
    elif role == "student":
        dest = "student_login"
    resp = redirect(url_for(dest))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/")
def index():
    return render_template("index.html", hide_nav=True)


@app.route("/dashboard")
@login_required
@admin_required
def dashboard():
    """Main admin dashboard with stats and charts."""
    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        
        # Safety: clear any stale aborted transaction (PostgreSQL).
        if str(app.config.get("DATABASE", "")).startswith("postgres"):
            try:
                db.rollback()
            except Exception:
                pass

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
        # Count unique conducted classes (unique combinations of date, subject, branch, and period)
        total_classes_query = "SELECT COUNT(*) FROM (SELECT 1 FROM attendance GROUP BY date, subject_id, branch_id, period) AS classes"
        total_classes = int(_safe_scalar(total_classes_query, default=0) or 0)

        attendance_stats = db.execute("""
            SELECT
                COUNT(CASE WHEN status='Present' THEN 1 END) as present_count,
                COUNT(*) as total_attendance_records
            FROM attendance
        """).fetchone()

        present_count = 0
        absent_count = 0
        attendance_record_count = 0
        try:
            present_count = int(row_get(attendance_stats, "present_count") or 0)
            attendance_record_count = int(row_get(attendance_stats, "total_attendance_records") or 0)
            absent_count = max(attendance_record_count - present_count, 0)
        except Exception:
            pass

        overall_percentage = 0
        if attendance_record_count > 0:
            overall_percentage = round((present_count / attendance_record_count) * 100, 1)

        # Additional analytics for upgraded dashboard
        today_str = date.today().isoformat()
        # Today's attendance
        today_q = f"SELECT COUNT(CASE WHEN status='Present' THEN 1 END) as present_today, COUNT(*) as total_today FROM attendance WHERE date = {placeholder}"
        try:
            today_row = db.execute(today_q, (today_str,)).fetchone()
            today_present = int(row_get(today_row, "present_today", 0) or 0)
            today_total = int(row_get(today_row, "total_today", 0) or 0)
            today_percentage = round((today_present / today_total) * 100, 1) if today_total > 0 else 0
        except Exception:
            today_present = 0
            today_total = 0
            today_percentage = 0

        # Active classes today (unique date, subject, branch, period)
        active_classes_today_q = f"SELECT COUNT(*) FROM (SELECT 1 FROM attendance WHERE date = {placeholder} GROUP BY subject_id, branch_id, period) AS classes"
        active_classes_today = int(_safe_scalar(active_classes_today_q, (today_str,), default=0) or 0)

        # Total teachers
        total_teachers = int(_safe_scalar("SELECT COUNT(*) FROM teachers", default=0) or 0)

        # Total semesters (distinct non-null current_semester in students)
        try:
            total_semesters = int(db.execute("SELECT COUNT(DISTINCT current_semester) AS cnt FROM students WHERE current_semester IS NOT NULL").fetchone()[0] or 0)
        except Exception:
            total_semesters = 0

        # Low attendance alerts: students with < 75% attendance (and at least 1 record)
        low_alerts_q = f"SELECT COUNT(*) FROM (SELECT s.id, SUM(CASE WHEN a.status='Present' THEN 1 ELSE 0 END) as present_marks, COUNT(a.id) as total_marks, (SUM(CASE WHEN a.status='Present' THEN 1 ELSE 0 END)*100.0)/NULLIF(COUNT(a.id),0) as pct FROM students s LEFT JOIN attendance a ON s.id = a.student_id GROUP BY s.id) sub WHERE sub.total_marks > 0 AND sub.pct < 75"
        low_attendance_alerts = int(_safe_scalar(low_alerts_q, default=0) or 0)

        # Monthly attendance trend (last 12 months) - use YYYY-MM substring for portability
        monthly_rows = _safe_fetchall(
            """
            SELECT substr(date,1,7) AS month,
                   SUM(CASE WHEN status='Present' THEN 1 ELSE 0 END) AS present_marks,
                   COUNT(*) AS total_marks
            FROM attendance
            GROUP BY month
            ORDER BY month DESC
            LIMIT 12
            """
        )
        monthly_labels = []
        monthly_percentages = []
        for row in reversed(monthly_rows):
            m = row_get(row, "month", "") or ""
            tm = int(row_get(row, "total_marks", 0) or 0)
            pm = int(row_get(row, "present_marks", 0) or 0)
            pct = round((pm / tm) * 100, 1) if tm > 0 else 0
            monthly_labels.append(m)
            monthly_percentages.append(pct)

        # Recent activity (last 10 attendance records)
        recent_activity = _safe_fetchall(
            f"""
            SELECT a.date, s.name AS student_name, sub.name AS subject_name, b.name AS branch_name, a.status
            FROM attendance a
            LEFT JOIN students s ON a.student_id = s.id
            LEFT JOIN subjects sub ON a.subject_id = sub.id
            LEFT JOIN branches b ON a.branch_id = b.id
            ORDER BY a.id DESC
            LIMIT 10
            """
        )

        subject_rows = _safe_fetchall(
            """
            SELECT
                s.name AS subject_name,
                (SELECT COUNT(*) FROM (SELECT 1 FROM attendance a2 WHERE a2.subject_id = s.id GROUP BY a2.date, a2.branch_id, a2.period) sub) AS total_count,
                SUM(CASE WHEN a.status = 'Present' THEN 1 ELSE 0 END) AS total_present_marks,
                COUNT(a.id) AS total_attendance_records
            FROM subjects s
            LEFT JOIN attendance a ON s.id = a.subject_id
            GROUP BY s.id, s.name
            ORDER BY s.name
            """
        )

        subject_chart_labels = []
        subject_chart_percentages = []
        for row in subject_rows:
            total_recs = int(row_get(row, "total_attendance_records", 0) or 0)
            present_mks = int(row_get(row, "total_present_marks", 0) or 0)
            pct = round((present_mks / total_recs) * 100, 1) if total_recs > 0 else 0
            subject_chart_labels.append(row_get(row, "subject_name", "") or "")
            subject_chart_percentages.append(pct)

        trend_rows = _safe_fetchall(
            """
            SELECT
                date,
                (SELECT COUNT(*) FROM (SELECT 1 FROM attendance a2 WHERE a2.date = a.date GROUP BY a2.subject_id, a2.branch_id, a2.period) sub) AS daily_classes,
                SUM(CASE WHEN status = 'Present' THEN 1 ELSE 0 END) AS present_marks,
                COUNT(*) AS total_marks
            FROM attendance a
            GROUP BY date
            ORDER BY date DESC
            LIMIT 14
            """
        )
        trend_labels = []
        trend_percentages = []
        for row in reversed(trend_rows):
            date_val = row_get(row, "date", "")
            total_m = int(row_get(row, "total_marks", 0) or 0)
            present_m = int(row_get(row, "present_marks", 0) or 0)
            pct = round((present_m / total_m) * 100, 1) if total_m > 0 else 0
            trend_labels.append(date_val)
            trend_percentages.append(pct)

        branch_data = _safe_fetchall(
            """
            SELECT
                b.id,
                b.name AS branch_name,
                b.location,
                (SELECT COUNT(*) FROM students s WHERE s.branch_id = b.id) AS student_count,
                (SELECT COUNT(*) FROM subjects sb WHERE sb.branch_id = b.id) AS subject_count,
                (SELECT COUNT(*) FROM (SELECT 1 FROM attendance a2 WHERE a2.branch_id = b.id GROUP BY a2.date, a2.subject_id, a2.period) sub) AS total_count,
                (SELECT COUNT(*) FROM attendance a3 WHERE a3.branch_id = b.id AND a3.status = 'Present') AS present_marks,
                (SELECT COUNT(*) FROM attendance a4 WHERE a4.branch_id = b.id) AS total_marks
            FROM branches b
            ORDER BY b.name
            """
        )

        processed_branch_data = []
        for row in branch_data:
            d = dict(row)
            tm = int(d.get("total_marks", 0) or 0)
            pm = int(d.get("present_marks", 0) or 0)
            d["attendance_percentage"] = round((pm / tm) * 100, 1) if tm > 0 else 0
            processed_branch_data.append(d)
        branch_data = processed_branch_data
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

        current_active_period = None
        upcoming_timetable = []
        try:
            import timetable as _timetable
            current_active_period = _timetable.get_global_active_class(db)
            upcoming_timetable = _timetable.get_upcoming_classes(db, "", "", limit=4)
        except Exception as timetable_err:
            print(f"[dashboard] timetable widget load skipped: {repr(timetable_err)}")

        db.close()
        database_info = {
            "storage": "PostgreSQL" if str(app.config.get("DATABASE", "")).startswith("postgresql") else "SQLite",
            "path": app.config.get("DATABASE", "unknown"),
        }
        mail_info = {
            "configured": is_mail_configured(),
            "server": app.config.get("MAIL_SERVER", "api.resend.com"),
            "port": app.config.get("MAIL_PORT", 443),
            "username": app.config.get("MAIL_FROM"),
            "tls": True,
            "render_env": bool(os.environ.get("RENDER") or os.environ.get("RENDER_INTERNAL_HOSTNAME")),
        }

        return render_template(
            "dashboard.html",
            branch_count=branch_count,
            student_count=student_count,
            subject_count=subject_count,
            attendance_count=attendance_record_count,
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
            today_percentage=today_percentage,
            today_present=today_present,
            today_total=today_total,
            active_classes_today=active_classes_today,
            total_teachers=total_teachers,
            total_semesters=total_semesters,
            low_attendance_alerts=low_attendance_alerts,
            monthly_labels=monthly_labels,
            monthly_percentages=monthly_percentages,
            recent_activity=recent_activity,
            current_active_period=current_active_period,
            upcoming_timetable=upcoming_timetable,
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
            today_percentage=0,
            today_present=0,
            today_total=0,
            active_classes_today=0,
            total_teachers=0,
            total_semesters=0,
            low_attendance_alerts=0,
            monthly_labels=[],
            monthly_percentages=[],
            recent_activity=[],
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
            ORDER BY COALESCE(students.import_order, students.id), students.id
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


@app.route("/api/low-attendance-details")
@admin_required
def api_low_attendance_details():
    """Return detailed low attendance information for dashboard modal."""
    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        threshold = get_setting(db, "low_attendance_threshold", app.config["LOW_ATTENDANCE_THRESHOLD"])
        
        # Query all students with their attendance details, including subject and semester
        query = f"""
            SELECT 
                s.id AS student_id,
                s.name AS student_name,
                s.roll_no AS roll_number,
                s.section AS section,
                s.current_semester AS semester,
                b.name AS branch_name,
                subj.name AS subject_name,
                COUNT(a.id) AS total_classes,
                SUM(CASE WHEN a.status = 'Present' THEN 1 ELSE 0 END) AS classes_attended,
                ROUND(
                    100.0 * SUM(CASE WHEN a.status = 'Present' THEN 1 ELSE 0 END) / NULLIF(COUNT(a.id), 0),
                    1
                ) AS attendance_percentage
            FROM students s
            LEFT JOIN attendance a ON s.id = a.student_id
            LEFT JOIN branches b ON s.branch_id = b.id
            LEFT JOIN subjects subj ON a.subject_id = subj.id
            GROUP BY s.id, s.name, s.roll_no, s.section, s.current_semester, b.name, subj.name
            HAVING COUNT(a.id) > 0 AND (100.0 * SUM(CASE WHEN a.status = 'Present' THEN 1 ELSE 0 END) / NULLIF(COUNT(a.id), 0)) < {placeholder}
            ORDER BY attendance_percentage ASC, s.name ASC
        """
        
        rows = db.execute(query, (threshold,)).fetchall()
        
        low_attendance_students = []
        for row in rows:
            attendance_pct = row_get(row, "attendance_percentage") or 0
            low_attendance_students.append({
                "student_id": row_get(row, "student_id"),
                "student_name": row_get(row, "student_name"),
                "roll_number": row_get(row, "roll_number") or "N/A",
                "branch_name": row_get(row, "branch_name") or "N/A",
                "section": row_get(row, "section") or "N/A",
                "semester": row_get(row, "semester") or 0,
                "subject_name": row_get(row, "subject_name") or "N/A",
                "attendance_percentage": float(attendance_pct),
                "total_classes": int(row_get(row, "total_classes") or 0),
                "classes_attended": int(row_get(row, "classes_attended") or 0),
                "is_critical": float(attendance_pct) < 65,  # Critical if below 65%
            })
        
        return jsonify({
            "success": True,
            "threshold": threshold,
            "count": len(low_attendance_students),
            "students": low_attendance_students
        })
    
    except Exception as e:
        print(f"[api_low_attendance_details] ERROR: {repr(e)}")
        print(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500
    finally:
        if db:
            try: db.close()
            except: pass


@app.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
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
        "server": app.config.get("MAIL_SERVER", "api.resend.com"),
        "port": app.config.get("MAIL_PORT", 443),
        "username": app.config.get("MAIL_FROM"),
        "tls": True,
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
        flash("Email is not configured. Please set RESEND_API_KEY and MAIL_FROM.", "error")
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
    try:
        email_sent, error_msg = send_email_with_error(
            subject="Test Email: Attendance System",
            recipient=recipient,
            body=body,
        )
        if email_sent:
            flash(f"Test email sent to {recipient}.", "success")
        else:
            flash(f"Failed to send test email: {error_msg}", "error")
    except Exception as e:
        flash(f"Resend API error: {str(e)}", "error")

    return redirect(url_for("settings"))


@app.route('/debug-env')
@login_required
def debug_env():
    """Development-only route to inspect masked mail-related env detection.

    Visible only when not in production. Returns masked values and sources
    (env vs .env) without exposing secrets.
    """
    if is_prod:
        abort(404)
    if session.get("role") != "admin":
        return redirect(url_for("dashboard"))

    try:
        report = getattr(app, 'mail_config_report', None)
        if not report:
            # Ensure module is loaded and report is generated
            import mail_config
            report = mail_config.setup_mail_config(app)
        return jsonify({"ok": True, "report": report})
    except Exception as e:
        print(f"[debug-env] error: {repr(e)}")
        return jsonify({"ok": False, "error": "failed to generate debug report"}), 500


@app.route('/admin/email-diagnostics', methods=['POST'])
@login_required
def email_diagnostics():
    """Run email diagnostics and optionally send a test email.

    Returns structured JSON with checks for environment variables, internet
    connectivity (Resend API), optional SMTP connectivity/auth (if legacy
    MAIL_USERNAME/MAIL_PASSWORD are present), and a test send result.
    """
    if session.get("role") != "admin":
        return jsonify({"error": "unauthorized"}), 403

    import socket
    import smtplib

    logger = logging.getLogger("app.email.diagnostics")
    logger.info("Starting email diagnostics")

    results = {
        "env": {},
        "connectivity": {},
        "smtp": {},
        "test_send": {},
    }

    # Environment variables
    for var in ("MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_FROM", "RESEND_API_KEY"):
        val = os.environ.get(var)
        results["env"][var] = bool(val and str(val).strip())

    # Internet connectivity check to Resend API
    try:
        r = requests.get("https://api.resend.com/", timeout=5)
        results["connectivity"]["resend_api"] = {"ok": True, "status_code": r.status_code}
    except Exception as e:
        results["connectivity"]["resend_api"] = {"ok": False, "error": str(e)}
        logger.warning("Resend connectivity check failed: %s", repr(e))

    # SMTP connectivity/auth checks only if legacy credentials present
    mail_username = os.environ.get("MAIL_USERNAME")
    mail_password = os.environ.get("MAIL_PASSWORD")
    mail_server = os.environ.get("MAIL_SERVER") or app.config.get("MAIL_SERVER") or "smtp.gmail.com"
    try:
        mail_port = int(os.environ.get("MAIL_PORT") or app.config.get("MAIL_PORT") or 587)
    except Exception:
        mail_port = 587

    smtp_report = {}
    if mail_username and mail_password:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(6)
        try:
            sock.connect((mail_server, mail_port))
            smtp_report["connection"] = {"ok": True, "message": f"Connected to {mail_server}:{mail_port}"}
            try:
                if mail_port == 465:
                    server = smtplib.SMTP_SSL(mail_server, mail_port, timeout=6)
                else:
                    server = smtplib.SMTP(mail_server, mail_port, timeout=6)
                    server.ehlo()
                    if app.config.get("MAIL_USE_TLS"):
                        server.starttls()
                        server.ehlo()
                try:
                    server.login(mail_username, mail_password)
                    smtp_report["auth"] = {"ok": True}
                except smtplib.SMTPAuthenticationError as aerr:
                    smtp_report["auth"] = {"ok": False, "error": str(aerr)}
                except Exception as e:
                    smtp_report["auth"] = {"ok": False, "error": str(e)}
                try:
                    server.quit()
                except Exception:
                    pass
            except Exception as e:
                smtp_report["auth"] = {"ok": False, "error": str(e)}
        except socket.timeout:
            smtp_report["connection"] = {"ok": False, "error": "Connection timed out (possible hosting provider blocking SMTP ports)"}
        except Exception as e:
            smtp_report["connection"] = {"ok": False, "error": str(e)}
        finally:
            try:
                sock.close()
            except Exception:
                pass
    else:
        smtp_report["skipped"] = "MAIL_USERNAME or MAIL_PASSWORD not set; SMTP checks skipped"

    results["smtp"] = smtp_report

    # Send a test email using the app's configured method (Resend wrapper)
    recipient = (app.config.get("REPORT_ADMIN_EMAIL") or os.environ.get("REPORT_ADMIN_EMAIL") or "").strip()
    if recipient and is_valid_email(recipient):
        try:
            ok, err = send_email_with_error(
                subject="Test Email: Attendance System",
                recipient=recipient,
                body=("This is a test email sent by the Email Diagnostics tool.\n\n"
                      f"Time: {date.today().isoformat()}\n"),
            )
            if ok:
                results["test_send"] = {"ok": True, "message": f"Test email sent to {recipient}"}
            else:
                results["test_send"] = {"ok": False, "error": err}
        except Exception as e:
            results["test_send"] = {"ok": False, "error": str(e)}
            logger.exception("Test send failed")
    else:
        results["test_send"] = {"ok": False, "error": "Admin recipient invalid or not set (REPORT_ADMIN_EMAIL)"}

    # Terminal log for debugging (full structured output)
    print("[email.diagnostics] ", results)
    logger.info("Email diagnostics completed")

    # Build friendly UI messages
    messages = []
    conn = results.get("connectivity", {}).get("resend_api")
    if conn and conn.get("ok"):
        messages.append("Internet connectivity to Resend API: OK")
    else:
        err = conn.get("error") if conn else "Unknown"
        messages.append("Internet connectivity to Resend API: Failed")
        if err:
            messages.append(f"Detail: {err}")

    smtp_conn = results.get("smtp", {}).get("connection")
    if smtp_conn:
        if smtp_conn.get("ok"):
            messages.append("SMTP connection: Success")
        else:
            err = smtp_conn.get("error", "")
            if "timed out" in str(err).lower():
                messages.append("SMTP connection: Failed — SMTP connection blocked by hosting provider")
            else:
                messages.append(f"SMTP connection: Failed — {err}")
    else:
        if results.get("smtp", {}).get("skipped"):
            messages.append("SMTP checks skipped: MAIL_USERNAME or MAIL_PASSWORD not set")

    auth = results.get("smtp", {}).get("auth")
    if auth:
        if auth.get("ok"):
            messages.append("SMTP authentication: Success")
        else:
            messages.append(f"SMTP authentication: Failed — {auth.get('error')}")

    # Environment variables summary
    for k, v in results.get("env", {}).items():
        messages.append(f"{k}: {'Loaded' if v else 'Not set'}")

    ts = results.get("test_send", {})
    if ts.get("ok"):
        messages.append("Test email sent successfully")
    else:
        messages.append(f"Test email failed: {ts.get('error')}")

    return jsonify({"results": results, "messages": messages})


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
@admin_required
def branches():
    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()

        if request.method == "POST":
            action = (request.form.get("action") or "add").strip().lower()
            branch_id = (request.form.get("branch_id") or "").strip()
            name = _normalize_branch_name(request.form.get("name"))
            location = request.form.get("location")
            sections = _normalize_branch_name(request.form.get("sections"))

            if action == "delete":
                if not branch_id:
                    flash("Branch ID is required for deletion.", "error")
                    return redirect(url_for("branches"))
                else:
                    refs = {
                        "students": db.execute(f"SELECT COUNT(*) AS count FROM students WHERE branch_id = {placeholder}", (branch_id,)).fetchone(),
                        "subjects": db.execute(f"SELECT COUNT(*) AS count FROM subjects WHERE branch_id = {placeholder}", (branch_id,)).fetchone(),
                        "attendance": db.execute(f"SELECT COUNT(*) AS count FROM attendance WHERE branch_id = {placeholder}", (branch_id,)).fetchone(),
                        "teacher_branches": db.execute(f"SELECT COUNT(*) AS count FROM teacher_branches WHERE branch_id = {placeholder}", (branch_id,)).fetchone(),
                    }
                    total_refs = sum(int(row_get(row, "count", 0) or 0) for row in refs.values())
                    if total_refs > 0:
                        flash("This branch cannot be deleted because it is still used by students, subjects, attendance, or teacher assignments.", "error")
                        return redirect(url_for("branches"))
                    else:
                        try:
                            db.execute(f"DELETE FROM branches WHERE id = {placeholder}", (branch_id,))
                            db.commit()
                            flash("Branch deleted successfully.", "success")
                            return redirect(url_for("branches"))
                        except Exception as e:
                            db.rollback()
                            print(f"[branches] delete error: {repr(e)}")
                            flash("Error deleting branch. See server logs.", "error")
                            return redirect(url_for("branches"))

            elif action == "edit":
                if not branch_id or not name:
                    flash("Branch ID and name are required for editing.", "error")
                    return redirect(url_for("branches"))
                else:
                    if _branch_name_exists(db, name, exclude_id=branch_id):
                        flash("Another branch already uses that name.", "error")
                        return redirect(url_for("branches"))
                    else:
                        try:
                            db.execute(
                                f"UPDATE branches SET name = {placeholder}, location = {placeholder} WHERE id = {placeholder}",
                                (name, location, branch_id),
                            )
                            db.commit()
                            flash("Branch updated successfully.", "success")
                            return redirect(url_for("branches"))
                        except Exception as e:
                            db.rollback()
                            print(f"[branches] update error: {repr(e)}")
                            flash("Error updating branch. See server logs.", "error")
                            return redirect(url_for("branches"))

            else:
                if not name:
                    flash("Branch name is required.", "error")
                    return redirect(url_for("branches"))
                else:
                    try:
                        branch_names = _build_branch_section_names(name, sections)
                        if not branch_names:
                            flash("Branch name is required.", "error")
                            return redirect(url_for("branches"))

                        added = 0
                        skipped = []
                        for branch_name in branch_names:
                            if _branch_name_exists(db, branch_name):
                                skipped.append(branch_name)
                                continue
                            db.execute(
                                f"INSERT INTO branches (name, location) VALUES ({placeholder}, {placeholder})",
                                (branch_name, location),
                            )
                            added += 1

                        db.commit()
                        if added and skipped:
                            flash(f"Added {added} section(s); skipped {len(skipped)} duplicate(s).", "warning")
                        elif added:
                            flash("Branch added successfully.", "success")
                        else:
                            flash("No new sections were added because they already exist.", "info")
                        return redirect(url_for("branches"))
                    except Exception as e:
                        db.rollback()
                        print(f"[branches] insert error: {repr(e)}")
                        flash("Error adding branch(s). See server logs.", "error")
                        return redirect(url_for("branches"))

        branches_list = db.execute("SELECT * FROM branches ORDER BY name").fetchall()
        response = make_response(render_template("branches.html", branches=branches_list))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
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
@admin_required
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
        subjects_list = db.execute("SELECT id, name FROM subjects ORDER BY name").fetchall()
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


@app.route("/delete_subject", methods=["POST"])
@login_required
@admin_required
def delete_subject():
    db = None
    try:
        subject_id = (request.form.get("subject_id") or "").strip()
        if not subject_id:
            flash("No subject specified for deletion.", "error")
            return redirect(url_for("subjects"))

        db = get_db()
        placeholder = get_placeholder()

        # Data Safety: Prevent accidental deletion if attendance records exist
        attendance_count_row = db.execute(f"SELECT COUNT(*) as count FROM attendance WHERE subject_id = {placeholder}", (subject_id,)).fetchone()
        attendance_count = row_get(attendance_count_row, 'count', 0)
        
        if attendance_count > 0:
            flash(f"Cannot delete subject because it has {attendance_count} attendance records. This ensures long-term data safety.", "error")
            return redirect(url_for("subjects"))

        # Delete the subject
        db.execute(f"DELETE FROM subjects WHERE id = {placeholder}", (subject_id,))
        db.commit()
        flash("Subject deleted successfully.", "success")
    except Exception as e:
        if db:
            try: db.rollback()
            except: pass
        print(f"[delete_subject] ERROR: {repr(e)}")
        flash("Failed to delete subject.", "error")
    finally:
        if db:
            try: db.close()
            except: pass
    return redirect(url_for("subjects"))


@app.route("/admin/teachers", methods=["GET", "POST"])
@login_required
@admin_required
def manage_teachers():
    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        
        if request.method == "POST":
            action = request.form.get("action")

            if action == "add":
                name = (request.form.get("name") or "").strip()
                username = (request.form.get("username") or "").strip()
                password = (request.form.get("password") or "").strip()
                subject_ids = [sid for sid in request.form.getlist("subject_ids") if str(sid).strip()]
                branch_ids = request.form.getlist("branch_ids")  # Multiple branches

                if not all([name, username, password, subject_ids, branch_ids]):
                    flash("Teacher name, username, password, at least one subject, and at least one branch are required.", "error")
                else:
                    # Duplicate username check
                    existing = db.execute(f"SELECT id FROM teachers WHERE username = {placeholder}", (username,)).fetchone()
                    if existing:
                        flash(f"Username '{username}' is already taken. Please choose a different username.", "error")
                    else:
                        # Look up subject name for backward compatibility
                        primary_subject_id = subject_ids[0]
                        sub_row = db.execute(f"SELECT name FROM subjects WHERE id = {placeholder}", (primary_subject_id,)).fetchone()
                        subject_name_val = row_get(sub_row, "name") if sub_row else ""
                        if not subject_name_val:
                            subject_name_val = ""
                        try:
                            # Insert teacher with first branch as legacy branch_id
                            first_branch_id = branch_ids[0] if branch_ids else None
                            first_branch_name, first_branch_section = _get_branch_name_and_section(db, first_branch_id)
                            db.execute(
                                f"INSERT INTO teachers (name, username, password, subject_id, subject_name, branch_id) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
                                (name, username, generate_password_hash(password), primary_subject_id, subject_name_val, first_branch_id)
                            )
                            db.commit()
                            
                            # Get the newly created teacher ID
                            teacher = db.execute(f"SELECT id FROM teachers WHERE username = {placeholder}", (username,)).fetchone()
                            new_teacher_id = row_get(teacher, "id")
                            
                            # Insert into teacher_branches for all selected branches
                            for branch_id in branch_ids:
                                try:
                                    db.execute(
                                        f"INSERT INTO teacher_branches (teacher_id, branch_id) VALUES ({placeholder}, {placeholder})",
                                        (new_teacher_id, branch_id)
                                    )
                                except Exception:
                                    pass  # Skip duplicate entries

                            for branch_id in branch_ids:
                                branch_name, branch_section = _get_branch_name_and_section(db, branch_id)
                                for subject_id in subject_ids:
                                    try:
                                        db.execute(
                                            f"INSERT INTO teacher_assignments (teacher_id, subject_id, branch_id, section) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
                                            (new_teacher_id, subject_id, branch_id, branch_section),
                                        )
                                    except Exception:
                                        pass

                            # Insert teacher subject assignments (junction table)
                            for subject_id in subject_ids:
                                try:
                                    db.execute(
                                        f"INSERT INTO teacher_subjects (teacher_id, subject_id) VALUES ({placeholder}, {placeholder})",
                                        (new_teacher_id, subject_id),
                                    )
                                except Exception:
                                    pass
                            db.commit()
                            flash(f"Teacher '{name}' added successfully with {len(subject_ids)} subject(s) and {len(branch_ids)} branch(es). They can now log in with username: {username}", "success")
                        except Exception as e:
                            db.rollback()
                            flash(f"Error adding teacher: {repr(e)}", "error")

            elif action == "edit":
                teacher_id = request.form.get("teacher_id")
                name = (request.form.get("name") or "").strip()
                username = (request.form.get("username") or "").strip()
                subject_ids = [sid for sid in request.form.getlist("subject_ids") if str(sid).strip()]
                branch_ids = request.form.getlist("branch_ids")  # Multiple branches

                if not all([teacher_id, name, username, subject_ids, branch_ids]):
                    flash("All fields including at least one subject and one branch are required.", "error")
                else:
                    # Check for duplicate username excluding self
                    dup = db.execute(f"SELECT id FROM teachers WHERE username = {placeholder} AND id != {placeholder}", (username, teacher_id)).fetchone()
                    if dup:
                        flash(f"Username '{username}' is already taken by another teacher.", "error")
                    else:
                        # Look up subject name to maintain backward compatibility
                        primary_subject_id = subject_ids[0]
                        sub_row = db.execute(f"SELECT name FROM subjects WHERE id = {placeholder}", (primary_subject_id,)).fetchone()
                        subject_name_val = row_get(sub_row, "name") if sub_row else ""
                        if not subject_name_val:
                            subject_name_val = ""
                        try:
                            first_branch_id = branch_ids[0] if branch_ids else None
                            first_branch_name, first_branch_section = _get_branch_name_and_section(db, first_branch_id)
                            db.execute(
                                f"UPDATE teachers SET name = {placeholder}, username = {placeholder}, subject_id = {placeholder}, subject_name = {placeholder}, branch_id = {placeholder} WHERE id = {placeholder}",
                                (name, username, primary_subject_id, subject_name_val, first_branch_id, teacher_id)
                            )
                            
                            # Clear existing branch assignments
                            db.execute(f"DELETE FROM teacher_branches WHERE teacher_id = {placeholder}", (teacher_id,))

                            # Clear existing subject assignments
                            try:
                                db.execute(f"DELETE FROM teacher_subjects WHERE teacher_id = {placeholder}", (teacher_id,))
                            except Exception:
                                pass
                            try:
                                db.execute(f"DELETE FROM teacher_assignments WHERE teacher_id = {placeholder}", (teacher_id,))
                            except Exception:
                                pass
                            
                            # Insert new branch assignments
                            for branch_id in branch_ids:
                                try:
                                    db.execute(
                                        f"INSERT INTO teacher_branches (teacher_id, branch_id) VALUES ({placeholder}, {placeholder})",
                                        (teacher_id, branch_id)
                                    )
                                except Exception:
                                    pass  # Skip duplicates

                            # Insert new subject assignments
                            for subject_id in subject_ids:
                                try:
                                    db.execute(
                                        f"INSERT INTO teacher_subjects (teacher_id, subject_id) VALUES ({placeholder}, {placeholder})",
                                        (teacher_id, subject_id),
                                    )
                                except Exception:
                                    pass

                            for branch_id in branch_ids:
                                branch_name, branch_section = _get_branch_name_and_section(db, branch_id)
                                for subject_id in subject_ids:
                                    try:
                                        db.execute(
                                            f"INSERT INTO teacher_assignments (teacher_id, subject_id, branch_id, section) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
                                            (teacher_id, subject_id, branch_id, branch_section),
                                        )
                                    except Exception:
                                        pass
                            
                            db.commit()
                            flash(f"Teacher '{name}' updated successfully with {len(subject_ids)} subject(s) and {len(branch_ids)} branch(es).", "success")
                        except Exception as e:
                            db.rollback()
                            flash(f"Error updating teacher: {repr(e)}", "error")

            elif action == "reset_password":
                teacher_id = request.form.get("teacher_id")
                new_password = (request.form.get("new_password") or "").strip()

                if not teacher_id or not new_password:
                    flash("Teacher ID and new password are required.", "error")
                elif len(new_password) < 4:
                    flash("Password must be at least 4 characters.", "error")
                else:
                    try:
                        db.execute(
                            f"UPDATE teachers SET password = {placeholder} WHERE id = {placeholder}",
                            (generate_password_hash(new_password), teacher_id)
                        )
                        db.commit()
                        flash("Password reset successfully.", "success")
                    except Exception as e:
                        db.rollback()
                        flash(f"Error resetting password: {repr(e)}", "error")

            elif action == "delete":
                teacher_id = request.form.get("teacher_id")
                if teacher_id:
                    attendance_count_row = db.execute(f"SELECT COUNT(*) as count FROM attendance WHERE teacher_id = {placeholder}", (teacher_id,)).fetchone()
                    attendance_count = row_get(attendance_count_row, 'count', 0)
                    if attendance_count > 0:
                        flash(f"Cannot delete teacher — they have {attendance_count} attendance record(s). This protects historical data.", "error")
                    else:
                        try:
                            db.execute(f"DELETE FROM teachers WHERE id = {placeholder}", (teacher_id,))
                            db.commit()
                            flash("Teacher deleted successfully.", "success")
                        except Exception as e:
                            db.rollback()
                            flash(f"Error deleting teacher: {repr(e)}", "error")

        teachers_list = db.execute("""
            SELECT DISTINCT t.id, t.name, t.username, t.subject_id, s.name AS subject_name
            FROM teachers t 
            LEFT JOIN subjects s ON t.subject_id = s.id 
            ORDER BY t.name
        """).fetchall()
        subjects_list = db.execute("SELECT id, name FROM subjects ORDER BY name").fetchall()
        branches_list = db.execute("SELECT id, name FROM branches ORDER BY name").fetchall()
        
        # For each teacher, fetch their assigned branches
        teacher_branches_map = {}
        teacher_subjects_map = {}
        for teacher_row in teachers_list:
            teacher_id = row_get(teacher_row, "id")
            assigned_branches = db.execute(f"""
                SELECT b.id, b.name
                FROM branches b
                JOIN teacher_branches tb ON b.id = tb.branch_id
                WHERE tb.teacher_id = {placeholder}
                ORDER BY b.name
            """, (teacher_id,)).fetchall()
            teacher_branches_map[teacher_id] = assigned_branches
            assigned_subjects = db.execute(f"""
                SELECT s.id, s.name
                FROM subjects s
                JOIN teacher_subjects ts ON s.id = ts.subject_id
                WHERE ts.teacher_id = {placeholder}
                ORDER BY s.name
            """, (teacher_id,)).fetchall()
            teacher_subjects_map[teacher_id] = assigned_subjects
        
        return render_template("admin_teachers.html", 
                             teachers=teachers_list, 
                             subjects=subjects_list, 
                             branches=branches_list,
                             teacher_branches_map=teacher_branches_map,
                             teacher_subjects_map=teacher_subjects_map)
    except Exception as e:
        print(f"[manage_teachers] ERROR: {repr(e)}")
        flash("Teacher management is temporarily unavailable.", "error")
        return redirect(url_for("dashboard"))
    finally:
        if db:
            try: db.close()
            except: pass


@app.route("/admin/academic", methods=["GET", "POST"])
@login_required
@admin_required
def admin_academic():
    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        
        if request.method == "POST":
            action = request.form.get("action")
            
            if action == "promote_semester":
                # Promote all students to next semester. If they are in sem 2, move to sem 1 of next year.
                # SQLite and Postgres handle this slightly differently, but standard SQL works.
                db.execute("""
                    UPDATE students 
                    SET 
                        current_year = CASE WHEN current_semester = 2 THEN current_year + 1 ELSE current_year END,
                        current_semester = CASE WHEN current_semester = 1 THEN 2 ELSE 1 END
                """)
                db.commit()
                flash("All students have been promoted to the next semester successfully.", "success")
                
            elif action == "promote_year":
                # Promote all students to next year, reset semester to 1
                db.execute("UPDATE students SET current_year = current_year + 1, current_semester = 1")
                db.commit()
                flash("All students have been promoted to the next academic year successfully.", "success")
                
            elif action == "update_student":
                student_id = request.form.get("student_id")
                new_year = request.form.get("current_year")
                new_sem = request.form.get("current_semester")
                if student_id and new_year and new_sem:
                    db.execute(f"UPDATE students SET current_year = {placeholder}, current_semester = {placeholder} WHERE id = {placeholder}", (new_year, new_sem, student_id))
                    db.commit()
                    flash("Student academic status updated.", "success")
                    
            elif action == "trigger_warnings":
                all_students = db.execute("SELECT id FROM students").fetchall()
                student_ids = [row_get(s, "id") for s in all_students]
                if student_ids:
                    dispatch_low_attendance_notifications(student_ids)
                    flash("Automated warnings triggered. Emails are being sent in the background to students below the attendance threshold.", "success")
                else:
                    flash("No students found to check.", "warning")

        # Fetch stats
        stats = db.execute("SELECT current_year, current_semester, COUNT(*) as count FROM students GROUP BY current_year, current_semester ORDER BY current_year, current_semester").fetchall()
        students = db.execute("SELECT id, name, enrollment, current_year, current_semester FROM students ORDER BY current_year, current_semester, name").fetchall()
        
        return render_template("admin_academic.html", stats=stats, students=students)
    except Exception as e:
        print(f"[admin_academic] ERROR: {repr(e)}")
        flash("Academic management is temporarily unavailable.", "error")
        return redirect(url_for("dashboard"))
    finally:
        if db:
            try: db.close()
            except: pass


@app.route("/upload_students", methods=["GET", "POST"])
@login_required
@admin_required
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
        max_order_row = db.execute("SELECT COALESCE(MAX(import_order), 0) AS max_import_order FROM students").fetchone()
        next_import_order = int(row_get(max_order_row, "max_import_order", 0) or 0) + 1

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
                name = _clean_text(row.get("name"))
                enrollment = _normalize_enrollment(row.get("enrollment"))
                email = _clean_text(row.get("email"))
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
                        INSERT INTO students (name, enrollment, email, branch_id, import_order)
                        VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
                        ON CONFLICT (enrollment) DO NOTHING
                        RETURNING id
                        """,
                        (name, enrollment, email_value, branch_id, next_import_order),
                    ).fetchone()
                    student_id = row_get(student_row, "id")
                    if not student_id:
                        skipped += 1
                        continue
                else:
                    cur = db.execute(
                        f"INSERT OR IGNORE INTO students (name, enrollment, email, branch_id, import_order) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
                        (name, enrollment, email_value, branch_id, next_import_order),
                    )
                    if getattr(cur, "rowcount", 0) == 0:
                        skipped += 1
                        continue
                    student_id = cur.lastrowid

                next_import_order += 1

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
    """CSV upload for students with enrollment sequence validation and safe batched inserts."""

    if request.method == "POST":
        file = request.files.get("file")
        if not file or not str(file.filename).lower().endswith(".csv"):
            flash("Please upload a valid CSV file.", "error")
            return redirect(url_for("upload_students_csv"))

        max_size_mb = int(os.environ.get("CSV_UPLOAD_MAX_MB", 20))
        try:
            stream = file.stream
            if hasattr(stream, "seek"):
                stream.seek(0, 2)
                file_size = stream.tell()
                stream.seek(0)
            else:
                file_size = 0

            if file_size and file_size > max_size_mb * 1024 * 1024:
                flash(f"File is too large ({file_size / 1024 / 1024:.1f}MB). Maximum is {max_size_mb}MB.", "error")
                return redirect(url_for("upload_students_csv"))
            if file_size:
                print(f"[CSV Upload] File size OK: {file_size / 1024:.1f}KB")
        except Exception as size_error:
            print(f"[CSV Upload] Could not check file size: {repr(size_error)}")

        db = None
        try:
            import csv
            import io
            import re

            try:
                from psycopg2.extras import execute_values
            except Exception:
                execute_values = None

            def _canon_header(value: object) -> str:
                return re.sub(r"[\s_\-]+", "", _clean_text(value).lstrip("\ufeff").lower())

            def _is_valid_email(email: str) -> bool:
                if not email:
                    return False
                pattern = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
                return re.match(pattern, email.strip()) is not None

            def _base36_char_to_int(ch: str):
                if not ch or len(ch) != 1:
                    return None
                if '0' <= ch <= '9':
                    return ord(ch) - ord('0')
                if 'A' <= ch <= 'Z':
                    return ord(ch) - ord('A') + 10
                return None

            def _int_to_base36_char(value: int):
                if value < 0 or value > 35:
                    return None
                if value < 10:
                    return chr(ord('0') + value)
                return chr(ord('A') + (value - 10))

            def _parse_enrollment_enhanced(enrollment: str):
                """
                Expected format support (as requested):
                - Prefix + 2-char sequence token where:
                  token[0] in 0-9,A-Z and token[1] in 0-9
                Examples:
                ...65, ...69, ...70, ...99, ...A0, ...A9, ...B0, ...C8

                Returns (prefix, sequence_index, sequence_token, mode)
                where sequence_index = base36(token[0]) * 10 + int(token[1]).
                """
                if not enrollment:
                    return None, None, None, None
                enrollment = enrollment.upper().strip()

                if len(enrollment) < 3:
                    return None, None, None, None

                prefix = enrollment[:-2]
                token_first = enrollment[-2]
                token_last = enrollment[-1]

                if not re.fullmatch(r"[A-Z0-9]+", prefix):
                    return None, None, None, None
                if not re.fullmatch(r"[0-9A-Z]", token_first):
                    return None, None, None, None
                if not re.fullmatch(r"[0-9]", token_last):
                    return None, None, None, None

                first_val = _base36_char_to_int(token_first)
                if first_val is None:
                    return None, None, None, None

                sequence_index = first_val * 10 + int(token_last)
                sequence_token = f"{token_first}{token_last}"
                return prefix, sequence_index, sequence_token, "tail2_alnum_digit"

            def _row_value(row_values, index):
                if index is None or index < 0 or index >= len(row_values):
                    return ""
                return _clean_text(row_values[index])

            dialect = csv.excel
            try:
                stream = file.stream
                if hasattr(stream, "seek"):
                    stream.seek(0)
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

            text_stream = io.TextIOWrapper(file.stream, encoding="utf-8-sig", newline="")
            reader = csv.reader(text_stream, dialect=dialect)

            headers = next(reader, None)
            print(f"[CSV Upload] Raw headers detected: {headers}")
            if not headers:
                flash("The uploaded CSV file is empty or unreadable.", "error")
                return redirect(url_for("upload_students_csv"))

            header_map = {}
            for index, header in enumerate(headers):
                key = _canon_header(header)
                if key and key not in header_map:
                    header_map[key] = index

            required_aliases = {
                "name": {"name", "studentname", "student"},
                "enrollment": {"enrollment", "enrollmentno", "htno", "hallticketno", "tendigitsh.t.no", "rollno"},
                "email": {"email", "mailid", "emailid", "mail"},
                "branch_id": {"branchid", "branch", "section"},
            }

            column_indexes = {}
            missing_columns = []
            for field_name, aliases in required_aliases.items():
                found_index = None
                for alias in aliases:
                    if alias in header_map:
                        found_index = header_map[alias]
                        break
                if found_index is None:
                    missing_columns.append(field_name)
                else:
                    column_indexes[field_name] = found_index

            if missing_columns:
                flash(
                    f"Missing required columns: {', '.join(sorted(missing_columns))}. CSV must include Name, Enrollment, Email, and Branch_ID.",
                    "error",
                )
                return redirect(url_for("upload_students_csv"))

            db = get_db()
            placeholder = get_placeholder()
            is_postgres = str(app.config.get("DATABASE", "")).startswith("postgres")
            if is_postgres and execute_values is None:
                raise RuntimeError("psycopg2.extras.execute_values is required for PostgreSQL CSV uploads")

            valid_branch_ids = set()
            for branch_row in db.execute("SELECT id FROM branches").fetchall():
                branch_id_value = row_get(branch_row, "id")
                if branch_id_value is not None:
                    try:
                        valid_branch_ids.add(int(branch_id_value))
                    except Exception:
                        pass

            existing_enrollments = set()
            for student_row in db.execute("SELECT enrollment FROM students").fetchall():
                enrollment_value = _normalize_enrollment(row_get(student_row, "enrollment"))
                if enrollment_value:
                    existing_enrollments.add(enrollment_value)

            inserted = 0
            skipped = 0
            duplicates = 0
            failed = 0
            missing_sequence_messages = []
            failed_rows_log = []
            seen_enrollments = set()
            password_hash_cache = {}
            pending_rows = []
            enrollments_by_prefix = {}
            max_order_row = db.execute("SELECT COALESCE(MAX(import_order), 0) AS max_import_order FROM students").fetchone()
            next_import_order = int(row_get(max_order_row, "max_import_order", 0) or 0) + 1

            for row_number, row_values in enumerate(reader, start=2):
                try:
                    row_values = list(row_values)
                    print(f"[CSV Upload] Processing row {row_number}: {row_values}")

                    if not row_values or not any(_clean_text(value) for value in row_values):
                        skipped += 1
                        print(f"[CSV Upload] Row {row_number}: skipped empty row")
                        continue

                    name = _row_value(row_values, column_indexes["name"])
                    raw_enrollment = _row_value(row_values, column_indexes["enrollment"])
                    enrollment = _normalize_enrollment(raw_enrollment)
                    email = _row_value(row_values, column_indexes["email"])
                    branch_id_raw = _row_value(row_values, column_indexes["branch_id"])

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

                    prefix, sequence_number, sequence_token, sequence_mode = _parse_enrollment_enhanced(enrollment)
                    if prefix is None:
                        msg = f"Row {row_number}: Invalid enrollment format '{enrollment}'"
                        print(f"[CSV Upload] {msg}")
                        failed_rows_log.append(msg)
                        failed += 1
                        continue

                    try:
                        branch_id = int(branch_id_raw)
                    except (TypeError, ValueError):
                        msg = f"Row {row_number}: Invalid branch_id '{branch_id_raw}'"
                        print(f"[CSV Upload] {msg}")
                        failed_rows_log.append(msg)
                        failed += 1
                        continue

                    if branch_id not in valid_branch_ids:
                        msg = f"Row {row_number}: Branch_ID {branch_id} does not exist"
                        print(f"[CSV Upload] {msg}")
                        failed_rows_log.append(msg)
                        failed += 1
                        continue

                    if not _is_valid_email(email):
                        msg = f"Row {row_number}: Invalid email format '{email}'"
                        print(f"[CSV Upload] {msg}")
                        failed_rows_log.append(msg)
                        failed += 1
                        continue

                    if enrollment in seen_enrollments or enrollment in existing_enrollments:
                        duplicates += 1
                        print(f"[CSV Upload] Row {row_number}: duplicate enrollment skipped ({enrollment})")
                        continue

                    seen_enrollments.add(enrollment)
                    key = (prefix, sequence_mode)
                    enrollments_by_prefix.setdefault(key, []).append(
                        {
                            "number": sequence_number,
                            "enrollment": enrollment,
                            "token": sequence_token,
                        }
                    )

                    password_plain = enrollment[-4:] if len(enrollment) >= 4 else enrollment
                    password_hash = password_hash_cache.get(password_plain)
                    if password_hash is None:
                        password_hash = generate_password_hash(password_plain, method="pbkdf2:sha256:120000")
                        password_hash_cache[password_plain] = password_hash

                    pending_rows.append(
                        {
                            "row_number": row_number,
                            "name": name,
                            "enrollment": enrollment,
                            "email": email,
                            "branch_id": branch_id,
                            "import_order": next_import_order,
                            "password_hash": password_hash,
                        }
                    )
                    next_import_order += 1
                    print(f"[CSV Upload] Row {row_number}: validated")

                except Exception as row_error:
                    failed += 1
                    msg = f"Row {row_number}: {repr(row_error)}"
                    print(f"[CSV Upload] FAILED ROW {row_number}: {row_values}")
                    print(f"[CSV Upload] EXCEPTION: {msg}")
                    print(traceback.format_exc())
                    failed_rows_log.append(msg)

            def _format_tail2_token_from_index(index_value: int):
                if index_value < 0:
                    return None
                first_val = index_value // 10
                last_digit = index_value % 10
                first_ch = _int_to_base36_char(first_val)
                if first_ch is None:
                    return None
                return f"{first_ch}{last_digit}"

            for (prefix, mode), items in enrollments_by_prefix.items():
                numbers = sorted({item["number"] for item in items})
                if len(numbers) < 2:
                    continue
                start, end = numbers[0], numbers[-1]
                full_range = set(range(start, end + 1))
                present = set(numbers)
                missing_numbers = sorted(full_range - present)
                for missing_number in missing_numbers:
                    if mode == "tail2_alnum_digit":
                        missing_suffix = _format_tail2_token_from_index(missing_number)
                        if missing_suffix is not None:
                            warning = f"Enrollment {prefix}{missing_suffix} is missing."
                        else:
                            warning = f"Enrollment {prefix}? is missing."
                    else:
                        warning = f"Enrollment {prefix}? is missing."
                    print(f"[CSV Upload] {warning}")
                    missing_sequence_messages.append(warning)

            if not pending_rows:
                summary = (
                    f"Upload complete! inserted 0, skipped {skipped}, duplicates {duplicates}, "
                    f"missing sequence numbers {len(missing_sequence_messages)}, failed rows {failed}."
                )
                print(f"[CSV Upload] {summary}")
                if missing_sequence_messages:
                    print("[CSV Upload] Sequence warnings:\n" + "\n".join(missing_sequence_messages[:20]))
                flash(summary, "warning" if (duplicates or missing_sequence_messages or failed) else "success")
                return redirect(url_for("students"))

            if is_postgres:
                conn = getattr(db, "_conn", None)
                if conn is None:
                    raise RuntimeError("PostgreSQL connection is not available")

                student_values = [
                    (row["name"], row["enrollment"], row["email"], row["branch_id"], row["import_order"])
                    for row in pending_rows
                ]

                with conn.cursor() as cur:
                    inserted_students = execute_values(
                        cur,
                        """
                        INSERT INTO students (name, enrollment, email, branch_id, import_order)
                        VALUES %s
                        ON CONFLICT (enrollment) DO NOTHING
                        RETURNING enrollment, id
                        """,
                        student_values,
                        page_size=200,
                        fetch=True,
                    ) or []

                    inserted_student_map = {row[0].upper(): row[1] for row in inserted_students}
                    user_values = []
                    for row in pending_rows:
                        student_id = inserted_student_map.get(row["enrollment"].upper())
                        if not student_id:
                            duplicates += 1
                            print(f"[CSV Upload] Row {row['row_number']}: skipped because student insert returned no id")
                            continue
                        user_values.append((row["enrollment"], row["password_hash"], "student", student_id))

                    if user_values:
                        execute_values(
                            cur,
                            """
                            INSERT INTO users (username, password, role, student_id)
                            VALUES %s
                            ON CONFLICT (username) DO NOTHING
                            """,
                            user_values,
                            page_size=200,
                        )

                inserted = len(inserted_students)
                if inserted < len(pending_rows):
                    duplicates += len(pending_rows) - inserted
            else:
                for row in pending_rows:
                    try:
                        cur = db.execute(
                            f"INSERT OR IGNORE INTO students (name, enrollment, email, branch_id, import_order) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
                            (row["name"], row["enrollment"], row["email"], row["branch_id"], row["import_order"]),
                        )
                        if getattr(cur, "rowcount", 0) == 0:
                            duplicates += 1
                            print(f"[CSV Upload] Row {row['row_number']}: duplicate enrollment skipped at insert stage ({row['enrollment']})")
                            continue

                        student_id = getattr(cur, "lastrowid", None)
                        if not student_id:
                            raise RuntimeError("Could not retrieve student ID after insert")

                        db.execute(
                            f"INSERT OR IGNORE INTO users (username, password, role, student_id) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
                            (row["enrollment"], row["password_hash"], "student", student_id),
                        )
                        inserted += 1
                    except Exception as insert_error:
                        failed += 1
                        msg = f"Row {row['row_number']}: {repr(insert_error)}"
                        print(f"[CSV Upload] FAILED ROW {row['row_number']}: {row}")
                        print(f"[CSV Upload] EXCEPTION: {msg}")
                        print(traceback.format_exc())
                        failed_rows_log.append(msg)

            db.commit()

            summary = (
                f"Upload complete! inserted {inserted}, skipped {skipped}, duplicates {duplicates}, "
                f"missing sequence numbers {len(missing_sequence_messages)}, failed rows {failed}."
            )
            print(f"[CSV Upload] {summary}")

            if missing_sequence_messages:
                print("[CSV Upload] Sequence warnings:\n" + "\n".join(missing_sequence_messages[:20]))

            if failed_rows_log:
                failed_preview = "\n".join(failed_rows_log[:5])
                if len(failed_rows_log) > 5:
                    failed_preview += f"\n... and {len(failed_rows_log) - 5} more errors"
                print(f"[CSV Upload] Failed rows:\n{failed_preview}")
                flash(f"{summary}\n\nWarnings:\n" + "\n".join(missing_sequence_messages[:10]) if missing_sequence_messages else summary, "warning")
                return redirect(url_for("students"))

            if missing_sequence_messages or duplicates:
                flash(summary + ("\n\n" + "\n".join(missing_sequence_messages[:10]) if missing_sequence_messages else ""), "warning")
            else:
                flash(summary, "success")

            return redirect(url_for("students"))

        except Exception as e:
            print(f"[CSV Upload CRITICAL] Unhandled exception: {repr(e)}")
            print(traceback.format_exc())
            if db:
                try:
                    db.rollback()
                    print("[CSV Upload] Database rolled back after critical error")
                except Exception as rollback_error:
                    print(f"[CSV Upload] Rollback failed: {repr(rollback_error)}")
            flash("Failed to process CSV file. Please check the file format and try again.", "error")
            return redirect(url_for("upload_students_csv"))
        finally:
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
            name = _clean_text(request.form.get("name"))
            enrollment = _normalize_enrollment(request.form.get("enrollment"))
            branch_id = request.form.get("branch_id", "").strip()
            email = request.form.get("email", "").strip()
            parent_email = request.form.get("parent_email", "").strip()
            branch_name, section = _get_branch_name_and_section(db, branch_id)

            if not name or not enrollment or not branch_id:
                flash("Name, enrollment, and branch are required.", "error")
            elif email and not is_valid_email(email):
                flash("Please enter a valid email address.", "error")
            elif parent_email and not is_valid_email(parent_email):
                flash("Please enter a valid parent email address.", "error")
            else:
                existing = db.execute(f"SELECT id FROM students WHERE enrollment = {placeholder}", (enrollment,)).fetchone()
                if existing:
                    flash("A student with this enrollment already exists.", "error")
                else:
                    try:
                        if str(app.config.get("DATABASE", "")).startswith("postgres"):
                            max_order_row = db.execute("SELECT COALESCE(MAX(import_order), 0) AS max_import_order FROM students").fetchone()
                            next_import_order = int(row_get(max_order_row, "max_import_order", 0) or 0) + 1
                            cur = db.execute(f"INSERT INTO students (name, enrollment, roll_no, email, parent_email, branch_id, section, import_order) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}) RETURNING id", (name, enrollment, enrollment, email or None, parent_email or None, branch_id, section, next_import_order))
                            student_id = cur.fetchone()[0]
                        else:
                            max_order_row = db.execute("SELECT COALESCE(MAX(import_order), 0) AS max_import_order FROM students").fetchone()
                            next_import_order = int(row_get(max_order_row, "max_import_order", 0) or 0) + 1
                            cur = db.execute(f"INSERT INTO students (name, enrollment, roll_no, email, parent_email, branch_id, section, import_order) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})", (name, enrollment, enrollment, email or None, parent_email or None, branch_id, section, next_import_order))
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
        query += " ORDER BY COALESCE(students.import_order, students.id), students.id"
        
        students_list = db.execute(query, params).fetchall()
        subjects_list = db.execute("SELECT id, name FROM subjects ORDER BY name").fetchall()
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


@app.route('/delete_student', methods=['POST'])
@login_required
@admin_required
def delete_student():
    """Delete a student and associated user record.

    Accepts either `student_id` or `enrollment` in form data. Requires admin.
    """
    db = None
    try:
        student_id = (request.form.get('student_id') or '').strip()
        enrollment = (request.form.get('enrollment') or '').strip()

        if not student_id and not enrollment:
            flash('No student specified for deletion.', 'error')
            return redirect(url_for('students'))

        db = get_db()
        placeholder = get_placeholder()

        # Lookup target student
        if student_id:
            target = db.execute(f"SELECT id, enrollment FROM students WHERE id = {placeholder}", (student_id,)).fetchone()
        else:
            target = db.execute(f"SELECT id, enrollment FROM students WHERE enrollment = {placeholder}", (enrollment,)).fetchone()

        if not target:
            flash('Student not found.', 'error')
            return redirect(url_for('students'))

        sid = row_get(target, 'id')
        enroll_val = row_get(target, 'enrollment') or ''

        # Data Safety: Prevent accidental deletion if attendance records exist
        attendance_count_row = db.execute(f"SELECT COUNT(*) as count FROM attendance WHERE student_id = {placeholder}", (sid,)).fetchone()
        attendance_count = row_get(attendance_count_row, 'count', 0)
        
        if attendance_count > 0:
            flash(f"Cannot delete student {enroll_val} because they have {attendance_count} attendance records. This ensures long-term data safety.", 'error')
            return redirect(url_for('students'))

        # Perform deletion inside a transaction for users and students only
        try:
            db.execute(f"DELETE FROM users WHERE student_id = {placeholder}", (sid,))
            db.execute(f"DELETE FROM students WHERE id = {placeholder}", (sid,))
            db.commit()
            flash(f"Student {enroll_val} deleted successfully.", 'success')
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            print(f"[delete_student] ERROR: {repr(e)}")
            flash('Failed to delete student. See server logs for details.', 'error')

        return redirect(url_for('students'))

    except Exception as e:
        print(f"[delete_student] Unexpected error: {repr(e)}")
        flash('An unexpected error occurred while deleting the student.', 'error')
        return redirect(url_for('students'))
    finally:
        if db:
            try: db.close()
            except: pass


@app.route('/bulk_delete_students', methods=['POST'])
@login_required
@admin_required
def bulk_delete_students():
    """Delete multiple students by a list of enrollments or IDs."""
    db = None
    try:
        raw_data = (request.form.get('bulk_data') or '').strip()
        if not raw_data:
            flash('No student data provided for bulk deletion.', 'error')
            return redirect(url_for('students'))

        import re
        # Split by comma, space, or newline
        items = re.split(r'[,\s\n]+', raw_data)
        items = [i.strip() for i in items if i.strip()]

        if not items:
            flash('No valid student IDs or enrollments found.', 'error')
            return redirect(url_for('students'))

        db = get_db()
        placeholder = get_placeholder()
        
        deleted_count = 0
        skipped_count = 0
        
        for item in items:
            try:
                # Try to find by enrollment first
                target = db.execute(f"SELECT id, enrollment FROM students WHERE enrollment = {placeholder}", (item,)).fetchone()
                # If not found by enrollment, try by numeric ID if the input is numeric
                if not target and item.isdigit():
                    target = db.execute(f"SELECT id, enrollment FROM students WHERE id = {placeholder}", (int(item),)).fetchone()
                
                if target:
                    sid = row_get(target, 'id')
                    db.execute(f"DELETE FROM attendance WHERE student_id = {placeholder}", (sid,))
                    db.execute(f"DELETE FROM users WHERE student_id = {placeholder}", (sid,))
                    db.execute(f"DELETE FROM students WHERE id = {placeholder}", (sid,))
                    db.commit()
                    deleted_count += 1
                else:
                    skipped_count += 1
            except Exception as e:
                print(f"[bulk_delete] Could not delete {item}: {repr(e)}")
                try:
                    db.rollback()
                except:
                    pass
                skipped_count += 1
        if deleted_count > 0:
            flash(f"Successfully deleted {deleted_count} students.", 'success')
        if skipped_count > 0:
            flash(f"Skipped {skipped_count} items (not found or error).", 'warning')
            
        return redirect(url_for('students'))
    except Exception as e:
        print(f"[bulk_delete_students] ERROR: {repr(e)}")
        flash('Bulk deletion failed.', 'error')
        return redirect(url_for('students'))
    finally:
        if db:
            try: db.close()
            except: pass


@app.route('/delete_all_students', methods=['POST'])
@login_required
@admin_required
def delete_all_students():
    """Permanently delete every student record in the system."""
    db = None
    try:
        db = get_db()
        # 1. Clear attendance first
        db.execute("DELETE FROM attendance")
        # 2. Clear student user accounts
        db.execute("DELETE FROM users WHERE role = 'student' OR student_id IS NOT NULL")
        # 3. Clear students table
        db.execute("DELETE FROM students")
        
        db.commit()
        flash("All student records have been permanently cleared.", "success")
        return redirect(url_for('students'))
    except Exception as e:
        if db:
            db.rollback()
        print(f"[delete_all_students] ERROR: {repr(e)}")
        flash("Failed to delete all students.", "error")
        return redirect(url_for('students'))
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

        # Normalize username to match stored enrollment (uppercase, no spaces)
        username = re.sub(r"\s+", "", username).upper()

        db = None
        try:
            db = get_db()
            placeholder = get_placeholder()
            user = db.execute(
                f"SELECT id, username, password, role, student_id FROM users WHERE username = {placeholder}",
                (username,),
            ).fetchone()

            password_ok = False
            if user and row_get(user, "role") == "student":
                stored = row_get(user, "password") or ""
                try:
                    password_ok = check_password_hash(stored, password)
                except Exception:
                    password_ok = (stored == password)
                    if password_ok:
                        try:
                            db.execute(
                                f"UPDATE users SET password = {placeholder} WHERE id = {placeholder}",
                                (generate_password_hash(password), row_get(user, "id")),
                            )
                            db.commit()
                        except Exception:
                            try:
                                db.rollback()
                            except Exception:
                                pass

            if user and row_get(user, "role") == "student" and password_ok:
                session.clear()
                session["user_id"] = row_get(user, "id")
                session["username"] = row_get(user, "username")
                session["role"] = row_get(user, "role")
                session["student_id"] = row_get(user, "student_id")
                session.permanent = True
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

    return render_template("student_login.html", next=next_url, hide_nav=True)


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
                f"SELECT id, username, password, name, subject_id FROM teachers WHERE username = {placeholder}",
                (username,),
            ).fetchone()

            password_ok = False
            assigned_branches = []
            assigned_subjects = []
            if user:
                stored = row_get(user, "password") or ""
                try:
                    password_ok = check_password_hash(stored, password)
                except Exception:
                    password_ok = (stored == password)
                    if password_ok:
                        try:
                            db.execute(
                                f"UPDATE teachers SET password = {placeholder} WHERE id = {placeholder}",
                                (generate_password_hash(password), row_get(user, "id")),
                            )
                            db.commit()
                        except Exception:
                            try:
                                db.rollback()
                            except Exception:
                                pass

                teacher_id = row_get(user, "id")
                assigned_branches = db.execute(f"""
                    SELECT b.id, b.name
                    FROM branches b
                    JOIN teacher_branches tb ON b.id = tb.branch_id
                    WHERE tb.teacher_id = {placeholder}
                    ORDER BY b.name
                """, (teacher_id,)).fetchall()
                assigned_subjects = db.execute(f"""
                    SELECT s.id, s.name, s.branch_id
                    FROM subjects s
                    JOIN teacher_subjects ts ON s.id = ts.subject_id
                    WHERE ts.teacher_id = {placeholder}
                    ORDER BY s.name
                """, (teacher_id,)).fetchall()

                if not assigned_subjects and row_get(user, "subject_id"):
                    fallback_subject = db.execute(
                        f"SELECT id, name, branch_id FROM subjects WHERE id = {placeholder}",
                        (row_get(user, "subject_id"),),
                    ).fetchone()
                    if fallback_subject:
                        assigned_subjects = [fallback_subject]

            if user and password_ok:
                teacher_id = row_get(user, "id")
                session.clear()
                session["user_id"] = teacher_id
                session["username"] = row_get(user, "username")
                session["role"] = "teacher"
                session["teacher_id"] = teacher_id
                session["teacher_name"] = row_get(user, "name")
                first_subject_id = row_get(assigned_subjects[0], "id") if assigned_subjects else row_get(user, "subject_id")
                if first_subject_id is not None:
                    session["teacher_subject_id"] = first_subject_id
                session.permanent = True

                if not assigned_subjects:
                    session.clear()
                    flash("No subjects assigned to your account. Contact admin.", "error")
                    return render_template("teacher_login.html")

                if not assigned_branches:
                    session.clear()
                    flash("No branches assigned to your account. Contact admin.", "error")
                    return render_template("teacher_login.html")
                
                # If teacher has multiple branches, show branch selection page
                if len(assigned_branches) > 1:
                    return redirect(url_for("teacher_select_branch"))
                elif len(assigned_branches) == 1:
                    session["teacher_branch_id"] = row_get(assigned_branches[0], "id")
                    return redirect(url_for("teacher_dashboard"))
                else:
                    # No branches assigned - show error
                    session.clear()
                    flash("No branches assigned to your account. Contact admin.", "error")
                    return render_template("teacher_login.html")

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

    return render_template("teacher_login.html", hide_nav=True)


@app.route("/teacher/select-branch", methods=["GET", "POST"])
@teacher_login_required
@teacher_required
def teacher_select_branch():
    """Allow teacher to select which branch to mark attendance for."""
    db = None
    try:
        db = get_db()
        teacher_id = session.get("teacher_id")
        placeholder = get_placeholder()
        
        if request.method == "POST":
            selected_branch_id = request.form.get("branch_id")
            selected_section = _normalize_branch_name(request.form.get("section"))
            if selected_branch_id:
                # Verify this branch/section pair is assigned to this teacher.
                assigned = db.execute(
                    f"""
                    SELECT id, section FROM teacher_assignments
                    WHERE teacher_id = {placeholder} AND branch_id = {placeholder}
                    """,
                    (teacher_id, selected_branch_id),
                ).fetchall()
                if not assigned:
                    assigned = db.execute(f"""
                        SELECT tb.branch_id AS branch_id, b.name AS branch_name
                        FROM teacher_branches tb
                        JOIN branches b ON b.id = tb.branch_id
                        WHERE tb.teacher_id = {placeholder} AND tb.branch_id = {placeholder}
                    """, (teacher_id, selected_branch_id)).fetchall()

                if assigned:
                    session["teacher_branch_id"] = selected_branch_id
                    branch_name, branch_section = _get_branch_name_and_section(db, selected_branch_id)
                    session["teacher_section"] = selected_section or branch_section or _branch_section_from_name(branch_name)
                    return redirect(url_for("teacher_dashboard"))
                else:
                    flash("Invalid branch selection.", "error")
        
        branches = _resolve_teacher_assignments(db, teacher_id)
        
        teacher = db.execute(
            f"SELECT name FROM teachers WHERE id = {placeholder}",
            (teacher_id,)
        ).fetchone()
        
        return render_template("teacher_select_branch.html", 
                             branches=branches,
                             teacher_name=row_get(teacher, "name") if teacher else "Teacher")
    except Exception as e:
        print(f"[teacher_select_branch] ERROR: {repr(e)}")
        flash("Error loading branch selection.", "error")
        return redirect(url_for("teacher_login"))
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@app.route("/teacher/select-subject", methods=["GET", "POST"])
@teacher_login_required
@teacher_required
def teacher_select_subject():
    """Allow teacher to select which assigned subject is active."""
    db = None
    try:
        db = get_db()
        teacher = get_teacher_context(db)
        if not teacher:
            return "Unauthorized Access", 403

        if request.method == "POST":
            selected_subject_id = (request.form.get("subject_id") or "").strip()
            allowed_ids = {
                str(row_get(subject, "id"))
                for subject in (teacher.get("assigned_subjects") or [])
                if row_get(subject, "id") is not None
            }

            if selected_subject_id and selected_subject_id in allowed_ids:
                session["teacher_subject_id"] = selected_subject_id
                return redirect(url_for("teacher_dashboard"))

            flash("Invalid subject selection.", "error")
            return redirect(url_for("teacher_select_subject"))

        return render_template(
            "teacher_select_subject.html",
            subjects=teacher.get("assigned_subjects") or [],
            teacher_name=teacher.get("name") or "Teacher",
            current_subject_id=teacher.get("current_subject_id"),
        )
    except Exception as e:
        print(f"[teacher_select_subject] ERROR: {repr(e)}")
        flash("Error loading subject selection.", "error")
        return redirect(url_for("teacher_dashboard"))
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@app.route("/teacher/dashboard")
@app.route("/teacher-dashboard")
@teacher_login_required
@teacher_required
def teacher_dashboard():
    db = None
    try:
        db = get_db()
        teacher = get_teacher_context(db)
        if not teacher:
            return "Unauthorized Access", 403

        placeholder = get_placeholder()
        subject_name = teacher["subject_name"]
        current_branch_id = teacher["current_branch_id"]
        current_branch_name = teacher["current_branch_name"] or ""
        current_section = teacher.get("current_section") or ""
        subject_row = teacher["subject_row"]
        subject_id = row_get(subject_row, "id") if subject_row else None

        if not current_branch_id:
            flash("No branch selected. Please select a branch.", "error")
            return redirect(url_for("teacher_select_branch"))

        student_count = db.execute(
            f"SELECT COUNT(*) AS count FROM students WHERE branch_id = {placeholder}",
            (current_branch_id,),
        ).fetchone()

        attendance_count = db.execute(
            f"SELECT COUNT(*) AS count FROM attendance WHERE subject_id = {placeholder} AND branch_id = {placeholder}",
            (subject_id, current_branch_id),
        ).fetchone()

        records = db.execute(
            f"""
            SELECT attendance.date, attendance.status, attendance.note,
                   students.name AS student_name, students.enrollment
            FROM attendance
            JOIN students ON attendance.student_id = students.id
            WHERE attendance.branch_id = {placeholder}
              AND attendance.subject_id = {placeholder}
                        ORDER BY COALESCE(students.import_order, students.id), students.id, attendance.id DESC
            LIMIT 20
            """,
            (current_branch_id, subject_id),
        ).fetchall()

        # Determine active slot for this teacher (prefer normalized timetable)
        active_slot = None
        try:
            import timetable as _timetable
            try:
                active_slot = _timetable.get_current_slot(db, current_branch_name or "", current_section or "")
            except Exception:
                active_slot = None
            try:
                global_active_slot = _timetable.get_global_active_class(db)
            except Exception:
                global_active_slot = None
            try:
                upcoming_classes = _timetable.get_upcoming_classes(db, "", "", limit=4)
            except Exception:
                upcoming_classes = []
        except Exception:
            active_slot = None
            global_active_slot = None
            upcoming_classes = []

        return render_template(
            "teacher_dashboard.html",
            teacher=teacher,
            student_count=row_get(student_count, "count", 0) or 0,
            attendance_count=row_get(attendance_count, "count", 0) or 0,
            recent_records=records,
            subject_id=subject_id,
            active_slot=active_slot,
            global_active_slot=global_active_slot,
            upcoming_classes=upcoming_classes,
        )
    except Exception as e:
        print(f"[teacher_dashboard] ERROR: {repr(e)}")
        try:
            import traceback as _tb
            print(_tb.format_exc())
        except Exception:
            pass
        # Log minimal session/teacher context to help debug transient failures
        try:
            print(f"[teacher_dashboard] session_keys={list(session.keys())}")
            print(f"[teacher_dashboard] teacher_context={repr(teacher)[:1000]}")
        except Exception:
            pass
        flash("Teacher dashboard is temporarily unavailable.", "error")
        return redirect(url_for("teacher_login"))
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@app.route("/teacher/attendance", methods=["GET", "POST"])
@app.route("/teacher-mark-attendance", methods=["GET", "POST"])
@teacher_login_required
@teacher_required
def teacher_mark_attendance():
    db = None
    try:
        db = get_db()
        teacher = get_teacher_context(db)
        if not teacher:
            return "Unauthorized Access", 403

        placeholder = get_placeholder()
        branch_request_id = (request.args.get("branch_id") or "").strip()
        subject_request_id = (request.args.get("subject_id") or "").strip()

        allowed_branch_ids = {
            str(row_get(branch, "id"))
            for branch in (teacher.get("assigned_branches") or [])
            if row_get(branch, "id") is not None
        }
        allowed_subject_ids = {
            str(row_get(subject, "id"))
            for subject in (teacher.get("assigned_subjects") or [])
            if row_get(subject, "id") is not None
        }

        if branch_request_id:
            if branch_request_id in allowed_branch_ids:
                session["teacher_branch_id"] = branch_request_id
            else:
                flash("Invalid branch selection.", "error")
                return "Unauthorized Access", 403

        if subject_request_id:
            if subject_request_id in allowed_subject_ids:
                session["teacher_subject_id"] = subject_request_id
            else:
                flash("Invalid subject selection.", "error")
                return "Unauthorized Access", 403

        if branch_request_id or subject_request_id:
            teacher = get_teacher_context(db)

        current_branch_id = teacher["current_branch_id"]
        current_branch_name = teacher["current_branch_name"]
        current_section = teacher.get("current_section") or _branch_section_from_name(current_branch_name or "") or current_branch_name or ""
        subject_name = teacher["subject_name"]
        subject_row = teacher["subject_row"]
        subject_id = row_get(subject_row, "id") if subject_row else None

        if not subject_row or subject_id is None or not current_branch_id:
            flash("No subject or branch selected.", "error")
            return redirect(url_for("teacher_select_branch"))

        today_str = date.today().isoformat()
        selected_date = request.args.get("date") or today_str
        period = request.args.get("period", "1")

        if request.method == "POST":
            selected_date = request.form.get("date") or today_str
            period = request.form.get("period", "1")
            student_ids = request.form.getlist("student_id")
            if not student_ids:
                flash("Please select at least one student.", "error")
            else:
                saved_ids = []
                blocked_overwrites = 0
                invalid_students = 0
                try:
                    for student_id in student_ids:
                        status = request.form.get(f"status_{student_id}", "Absent")
                        note = request.form.get(f"note_{student_id}", "")

                        # Validate student belongs to this branch
                        ok_student = db.execute(
                            f"SELECT 1 FROM students WHERE id = {placeholder} AND branch_id = {placeholder} AND (COALESCE(section, '') = COALESCE({placeholder}, '') OR COALESCE(section, '') = '')",
                            (student_id, current_branch_id, current_section),
                        ).fetchone()
                        if not ok_student:
                            invalid_students += 1
                            continue

                        # Do not allow overwriting attendance created by another teacher
                        existing = db.execute(
                            f"""
                            SELECT teacher_id
                            FROM attendance
                            WHERE student_id = {placeholder}
                              AND subject_id = {placeholder}
                              AND date = {placeholder}
                              AND period = {placeholder}
                            """,
                            (student_id, subject_id, selected_date, period),
                        ).fetchone()
                        existing_teacher_id = row_get(existing, "teacher_id") if existing else None
                        if existing_teacher_id and str(existing_teacher_id) != str(teacher["teacher_id"]):
                            blocked_overwrites += 1
                            continue

                        db.execute(
                            f"""
                            INSERT INTO attendance (
                                student_id, branch_id, branch_section, subject_id, teacher_id, subject_name,
                                date, period, status, note
                            ) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
                            ON CONFLICT (student_id, subject_id, date, period) DO UPDATE
                            SET status = EXCLUDED.status,
                                note = EXCLUDED.note,
                                teacher_id = EXCLUDED.teacher_id,
                                subject_name = EXCLUDED.subject_name,
                                branch_section = EXCLUDED.branch_section
                            """,
                            (student_id, current_branch_id, current_section, subject_id, teacher["teacher_id"], subject_name, selected_date, period, status, note),
                        )
                        if str(student_id).isdigit():
                            saved_ids.append(int(student_id))
                    db.commit()
                    if invalid_students:
                        flash(f"Skipped {invalid_students} invalid student(s).", "warning")
                    if blocked_overwrites:
                        flash(f"Skipped {blocked_overwrites} record(s) already owned by another teacher.", "warning")
                    flash(f"Attendance for Period {period} saved successfully.", "success")
                    return redirect(url_for("teacher_mark_attendance", date=selected_date, period=period))
                except Exception as save_error:
                    db.rollback()
                    print(f"[teacher_mark_attendance] ERROR: {repr(save_error)}")
                    flash("Failed to save attendance.", "error")

        students = db.execute(
            f"SELECT id, name, enrollment, roll_no, section FROM students WHERE branch_id = {placeholder} AND (COALESCE(section, '') = COALESCE({placeholder}, '') OR COALESCE(section, '') = '') ORDER BY COALESCE(import_order, id), id",
            (current_branch_id, current_section),
        ).fetchall()

        attendance_map = {}
        for row in db.execute(
            f"""
            SELECT student_id, status, note
            FROM attendance
            WHERE branch_id = {placeholder}
              AND subject_id = {placeholder}
              AND date = {placeholder}
              AND period = {placeholder}
            """,
            (current_branch_id, subject_id, selected_date, period),
        ).fetchall():
            attendance_map[str(row_get(row, "student_id"))] = row

        return render_template(
            "teacher_mark_attendance.html",
            teacher=teacher,
            students=students,
            attendance_map=attendance_map,
            selected_date=selected_date,
            period=period,
            today_date=today_str,
            current_section=current_section,
        )
    except Exception as e:
        print(f"[teacher_mark_attendance] ERROR: {repr(e)}")
        flash("Teacher attendance page is temporarily unavailable.", "error")
        return redirect(url_for("teacher_dashboard"))
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@app.route("/teacher/records")
@app.route("/teacher-records")
@teacher_login_required
@teacher_required
def teacher_attendance_records():
    db = None
    try:
        db = get_db()
        teacher = get_teacher_context(db)
        if not teacher:
            return "Unauthorized Access", 403

        placeholder = get_placeholder()
        subject_id = row_get(teacher["subject_row"], "id") if teacher["subject_row"] else None
        current_branch_id = teacher["current_branch_id"]
        search = (request.args.get("search") or "").strip()

        if not subject_id or not current_branch_id:
            flash("No subject or branch assigned.", "error")
            return redirect(url_for("teacher_dashboard"))

        query = (
            "SELECT attendance.date, attendance.status, attendance.note, attendance.subject_name, "
            "students.name AS student_name, students.enrollment, branches.name AS branch_name "
            "FROM attendance "
            "JOIN students ON attendance.student_id = students.id "
            "JOIN branches ON attendance.branch_id = branches.id "
            f"WHERE attendance.branch_id = {placeholder} AND attendance.subject_id = {placeholder}"
        )
        params = [current_branch_id, subject_id]
        if search:
            like_op = "ILIKE" if str(app.config.get("DATABASE", "")).startswith("postgres") else "LIKE"
            query += f" AND (students.name {like_op} {placeholder} OR students.enrollment {like_op} {placeholder})"
            params.extend([f"%{search}%", f"%{search}%"])
        query += " ORDER BY COALESCE(students.import_order, students.id), students.id, attendance.id DESC"

        records = db.execute(query, params).fetchall()
        return render_template(
            "teacher_records.html",
            teacher=teacher,
            records=records,
            search=search,
        )
    except Exception as e:
        print(f"[teacher_attendance_records] ERROR: {repr(e)}")
        flash("Teacher records are temporarily unavailable.", "error")
        return redirect(url_for("teacher_dashboard"))
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@app.route("/student_dashboard")
@app.route("/student/dashboard")
@student_login_required
@student_required
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
        
        # Calculate conducted sessions for this branch (and optionally subject)
        sessions_query = f"SELECT COUNT(*) FROM (SELECT 1 FROM attendance WHERE branch_id = {placeholder} "
        sessions_params = [row_get(student, "branch_id")]
        if selected_subject_id:
            sessions_query += f"AND subject_id = {placeholder} "
            sessions_params.append(selected_subject_id)
        sessions_query += "GROUP BY date, subject_id, branch_id, period) sub"
        
        total_conducted = db.execute(sessions_query, tuple(sessions_params)).fetchone()[0]
        
        present = len([a for a in attendance_records if row_get(a, "status") == "Present"])
        absent = total_conducted - present
        percentage = round((present / total_conducted) * 100, 1) if total_conducted > 0 else 0

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

        # Calculate subject-wise attendance analytics
        subject_analytics = []
        for subj in subjects:
            subj_id = row_get(subj, "id")
            subj_name = row_get(subj, "name")
            
            # Total conducted for this subject
            s_total_q = f"SELECT COUNT(*) FROM (SELECT 1 FROM attendance WHERE branch_id = {placeholder} AND subject_id = {placeholder} GROUP BY date, subject_id, branch_id, period) sub"
            s_total = db.execute(s_total_q, (row_get(student, "branch_id"), subj_id)).fetchone()[0]
            
            if s_total > 0:
                s_present = len([a for a in attendance_records if row_get(a, "subject_id") == subj_id and row_get(a, "status") == "Present"])
                subject_analytics.append({
                    "subject": subj_name,
                    "percentage": round((s_present / s_total) * 100, 1)
                })

        # Calculate monthly attendance analytics
        monthly_analytics_raw = {}
        for record in attendance_records:
            date_str = row_get(record, "date")
            if not date_str: continue
            month_prefix = date_str[:7] # YYYY-MM
            if month_prefix not in monthly_analytics_raw:
                monthly_analytics_raw[month_prefix] = {"present": 0, "total": 0}
            
            # We can't easily get total conducted per month per student from just attendance_records unless we query it.
            # But we can approximate based on recorded attendance for this student:
            monthly_analytics_raw[month_prefix]["total"] += 1
            if row_get(record, "status") == "Present":
                monthly_analytics_raw[month_prefix]["present"] += 1

        monthly_analytics = []
        for month in sorted(monthly_analytics_raw.keys()):
            data = monthly_analytics_raw[month]
            if data["total"] > 0:
                monthly_analytics.append({
                    "month": month,
                    "percentage": round((data["present"] / data["total"]) * 100, 1)
                })

        db.close()
        return render_template("student_dashboard.html", student=student, attendance_records=attendance_records, total_classes=total_conducted, present_count=present, absent_count=absent, percentage=percentage, subjects=subjects, selected_subject_id=selected_subject_id, student_qr_data_uri=student_qr_data_uri, subject_analytics=subject_analytics, monthly_analytics=monthly_analytics)
    except Exception as e:
        print(f"[student_dashboard] ERROR: {repr(e)}")
        if db:
            try: db.close()
            except: pass
        flash("Your dashboard is temporarily unavailable.", "error")
        return redirect(url_for("student_login"))


@app.route("/student_dashboard/<int:student_id>")
@login_required
@admin_required
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

    # Calculate conducted sessions for this branch (and optionally subject)
    sessions_query = f"SELECT COUNT(*) FROM (SELECT 1 FROM attendance WHERE branch_id = {placeholder} "
    sessions_params = [row_get(student, "branch_id")]
    if selected_subject_id:
        sessions_query += f"AND subject_id = {placeholder} "
        sessions_params.append(selected_subject_id)
    sessions_query += "GROUP BY date, subject_id, branch_id, period) sub"
    
    total_conducted = db.execute(sessions_query, tuple(sessions_params)).fetchone()[0]

    present = len([a for a in attendance_records if row_get(a, "status") == "Present"])
    percentage = round((present / total_conducted) * 100, 1) if total_conducted > 0 else 0

    db.close()
    return render_template(
        "student_dashboard.html",
        student=student,
        attendance_records=attendance_records,
        total_classes=total_conducted,
        percentage=percentage,
        subjects=subjects,
        selected_subject_id=selected_subject_id,
    )


@app.route("/mark_attendance", methods=["GET", "POST"])
@login_required
@admin_required
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
        period = request.args.get("period", "1")

        # 2. Handle POST (Saving Attendance)
        if request.method == "POST":
            # Re-read form data to avoid stale context
            branch_id = request.form.get("branch_id")
            subject_id = request.form.get("subject_id")
            selected_date = request.form.get("date") or today_str
            period = request.form.get("period", "1")
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
                                INSERT INTO attendance (student_id, branch_id, branch_section, subject_id, date, period, status, note)
                                VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
                                ON CONFLICT (student_id, subject_id, date, period) DO UPDATE 
                                SET status = EXCLUDED.status, note = EXCLUDED.note, branch_section = EXCLUDED.branch_section
                            """, (student_id, branch_id, _get_branch_section_name(db, branch_id), subject_id, selected_date, period, status, note))
                        else:
                            # SQLite manual update
                            db.execute(f"DELETE FROM attendance WHERE student_id={placeholder} AND subject_id={placeholder} AND date={placeholder} AND period={placeholder}", (student_id, subject_id, selected_date, period))
                            db.execute(f"INSERT INTO attendance (student_id, branch_id, branch_section, subject_id, date, period, status, note) VALUES ({placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder})", (student_id, branch_id, _get_branch_section_name(db, branch_id), subject_id, selected_date, period, status, note))
                        
                        if str(student_id).isdigit():
                            saved_ids.append(int(student_id))
                    
                    db.commit()
                    flash(f"Attendance for Period {period} saved successfully.", "success")
                    dispatch_low_attendance_notifications(saved_ids)
                    return redirect(url_for("attendance_success", branch_id=branch_id, subject_id=subject_id, date=selected_date, period=period))
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
            students = db.execute(f"SELECT id, name, enrollment FROM students WHERE branch_id = {placeholder} ORDER BY COALESCE(import_order, id), id", (branch_id,)).fetchall()
            att_rows = db.execute(f"SELECT student_id, status, note FROM attendance WHERE subject_id = {placeholder} AND date = {placeholder} AND period = {placeholder}", (subject_id, selected_date, period)).fetchall()
            attendance_map = {str(row_get(r, "student_id")): r for r in att_rows}

        prev_date = (date.fromisoformat(selected_date) - timedelta(days=1)).isoformat() if selected_date else today_str

        return render_template(
            "mark_attendance.html",
            branches=branches,
            subjects=subjects,
            students=students,
            branch_id=branch_id,
            subject_id=subject_id,
            selected_date=selected_date,
            period=period,
            attendance_map=attendance_map,
            today_date=today_str,
            prev_date=prev_date
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
@admin_required
def generate_qr():
    branch_id = request.args.get("branch_id")
    subject_id = request.args.get("subject_id")
    selected_date = request.args.get("date") or date.today().isoformat()
    period = request.args.get("period", "1")

    if not branch_id or not subject_id:
        flash("Please select a branch and subject before generating a QR code.", "error")
        return redirect(url_for("mark_attendance", branch_id=branch_id, subject_id=subject_id, date=selected_date, period=period))

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
        return redirect(url_for("mark_attendance", branch_id=branch_id, subject_id=subject_id, date=selected_date, period=period))

    return render_template(
        "qr_display.html",
        branch_id=branch_id,
        subject_id=subject_id,
        branch_name=branch["name"],
        subject_name=subject["name"],
        date=selected_date,
        period=period
    )


@app.route("/attendance/scan")
def attendance_scan():
    branch_id = request.args.get("branch_id")
    subject_id = request.args.get("subject_id")
    selected_date = request.args.get("date") or date.today().isoformat()
    period = request.args.get("period", "1")

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
        f"SELECT id, status FROM attendance WHERE student_id = {placeholder} AND subject_id = {placeholder} AND date = {placeholder} AND period = {placeholder}",
        (student_id, subject_id, selected_date, period),
    ).fetchone()

    if existing:
        if row_get(existing, "status") != "Present":
            db.execute(
                f"UPDATE attendance SET status = {placeholder}, note = {placeholder}, branch_section = {placeholder} WHERE id = {placeholder}",
                ("Present", "Marked via QR scan", row_get(branch, "name"), row_get(existing, "id")),
            )
            db.commit()
            message = "Your attendance has been updated to Present."
        else:
            message = "Your attendance is already marked as Present."
    else:
        db.execute(
            f"INSERT INTO attendance (student_id, branch_id, branch_section, subject_id, date, period, status, note) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
            (student_id, branch_id, row_get(branch, "name"), subject_id, selected_date, period, "Present", "Marked via QR scan"),
        )
        db.commit()
        message = "Attendance recorded successfully."

    db.close()

    return render_template(
        "attendance_scan.html",
        branch_name=row_get(branch, "name"),
        subject_name=row_get(subject, "name"),
        date=selected_date,
        period=period,
        message=message
    )


@app.route("/api/generate_qr_token")
@login_required
def generate_qr_token():
    branch_id = request.args.get("branch_id")
    subject_id = request.args.get("subject_id")
    selected_date = request.args.get("date") or date.today().isoformat()
    period = request.args.get("period", "1")

    if not branch_id or not subject_id:
        return jsonify({"error": "branch_id and subject_id are required."}), 400

    scan_url = url_for(
        "attendance_scan",
        branch_id=branch_id,
        subject_id=subject_id,
        date=selected_date,
        period=period,
        _external=True,
    )
    return jsonify({"scan_url": scan_url})


@app.route("/attendance/success")
@login_required
def attendance_success():
    branch_id = request.args.get("branch_id") or ""
    subject_id = request.args.get("subject_id") or ""
    selected_date = request.args.get("date") or date.today().isoformat()
    period = request.args.get("period", "1")
    db = get_db()
    placeholder = get_placeholder()
    branch = db.execute(f"SELECT name FROM branches WHERE id = {placeholder}", (branch_id,)).fetchone()
    subject = db.execute(f"SELECT name FROM subjects WHERE id = {placeholder}", (subject_id,)).fetchone()
    attendance_count = db.execute(
        f"SELECT COUNT(*) AS count FROM attendance WHERE branch_id = {placeholder} AND subject_id = {placeholder} AND date = {placeholder} AND period = {placeholder}",
        (branch_id, subject_id, selected_date, period),
    ).fetchone()["count"]
    db.close()

    email_summary = session.pop("attendance_email_summary", [])
    mail_configured = is_mail_configured()

    return render_template(
        "attendance_success.html",
        branch_name=row_get(branch, "name"),
        subject_name=row_get(subject, "name"),
        selected_date=selected_date,
        period=period,
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
        "year": request.args.get("year") or request.form.get("year"),
        "semester": request.args.get("semester") or request.form.get("semester"),
    }


def fetch_report_records(db, filters):
    placeholder = get_placeholder()
    query = (
        "SELECT attendance.*, students.name AS student_name, students.enrollment, "
        "COALESCE(attendance.branch_section, branches.name) AS branch_name, subjects.name AS subject_name "
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

    if filters.get("year"):
        like_op = "ILIKE" if str(app.config.get("DATABASE", "")).startswith("postgres") else "LIKE"
        clauses.append(f"attendance.date {like_op} {placeholder}")
        params.append(f"{filters['year']}-%")
    
    if filters.get("semester"):
        sem = filters["semester"]
        like_op = "ILIKE" if str(app.config.get("DATABASE", "")).startswith("postgres") else "LIKE"
        if sem == "1":
            # Semester 1: Jan to Jun
            clauses.append(f"(attendance.date {like_op} {placeholder} OR attendance.date {like_op} {placeholder} OR attendance.date {like_op} {placeholder} OR attendance.date {like_op} {placeholder} OR attendance.date {like_op} {placeholder} OR attendance.date {like_op} {placeholder})")
            params.extend(["%-01-%", "%-02-%", "%-03-%", "%-04-%", "%-05-%", "%-06-%"])
        elif sem == "2":
            # Semester 2: Jul to Dec
            clauses.append(f"(attendance.date {like_op} {placeholder} OR attendance.date {like_op} {placeholder} OR attendance.date {like_op} {placeholder} OR attendance.date {like_op} {placeholder} OR attendance.date {like_op} {placeholder} OR attendance.date {like_op} {placeholder})")
            params.extend(["%-07-%", "%-08-%", "%-09-%", "%-10-%", "%-11-%", "%-12-%"])

    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    query += " ORDER BY COALESCE(students.import_order, students.id), students.id, attendance.id"
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
                "Period": row_get(record, "period") or "1",
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

    table_data = [["Name", "Enrollment", "Subject", "Date", "Period", "Status"]]
    for r in records:
        table_data.append(
            [
                str(row_get(r, "student_name") or ""),
                str(row_get(r, "enrollment") or ""),
                str(row_get(r, "subject_name") or ""),
                str(row_get(r, "date") or ""),
                str(row_get(r, "period") or "1"),
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
@admin_required
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
        students = db.execute("SELECT id, name FROM students ORDER BY COALESCE(import_order, id), id").fetchall()
        
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
@admin_required
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
@admin_required
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
        flash("Email is not configured. Please set RESEND_API_KEY and MAIL_FROM.", "error")
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
            SELECT a.date, s.name as student_name, sub.name as subject_name, a.status, COALESCE(a.branch_section, b.name) AS branch_name
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

    timeout = float(os.environ.get("RESEND_TIMEOUT_SECONDS", 8))
    api_key = (app.config.get("RESEND_API_KEY") or "").strip()
    host = app.config.get('MAIL_SERVER', 'api.resend.com')

    if not api_key:
        return jsonify({'ok': False, 'provider': 'resend', 'server': host, 'error': 'RESEND_API_KEY not configured'})

    try:
        response = requests.get(
            'https://api.resend.com/domains',
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=timeout,
        )
        if 200 <= response.status_code < 300:
            return jsonify({'ok': True, 'provider': 'resend', 'server': host, 'message': 'Resend API connection successful'})
        return jsonify({
            'ok': False,
            'provider': 'resend',
            'server': host,
            'status': response.status_code,
            'error': (response.text or '')[:500],
        })
    except requests.RequestException as e:
        return jsonify({'ok': False, 'provider': 'resend', 'server': host, 'error': str(e)})


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


@app.route("/attendance-analytics")
@app.route("/attendance_analytics")
@login_required
def attendance_analytics():
    # Lightweight compatibility route so existing dashboard links render safely.
    # Detailed analytics were moved into the reports module.
    return redirect(url_for("reports_index"))
    print(f"[CRITICAL] 500 ERROR: {repr(error)}")
    print(traceback.format_exc())
    return "<h1>Internal Server Error</h1><p>Our team has been notified. Please try again later.</p>", 500

@app.errorhandler(404)
def not_found_error(error):
    return "<h1>404 Not Found</h1><p>The page you requested does not exist.</p>", 404

def _parse_date_param(val):
    if not val:
        return None
    try:
        return date.fromisoformat(val)
    except Exception:
        try:
            return date.fromtimestamp(int(val))
        except Exception:
            return None


def _parse_int_param(val):
    if val is None or val == "":
        return None
    try:
        return int(val)
    except Exception:
        return None


def _exists_id(db, table, id_val):
    try:
        placeholder = get_placeholder()
        row = db.execute(f"SELECT 1 FROM {table} WHERE id = {placeholder}", (id_val,)).fetchone()
        return bool(row)
    except Exception:
        return False


def _get_report_rows(db, subject_id=None, branch_id=None, start_date=None, end_date=None):
    placeholder = get_placeholder()
    where_clauses = []
    params = []

    if subject_id:
        where_clauses.append(f"attendance.subject_id = {placeholder}")
        params.append(subject_id)
    if branch_id:
        where_clauses.append(f"attendance.branch_id = {placeholder}")
        params.append(branch_id)
    if start_date:
        where_clauses.append(f"attendance.date >= {placeholder}")
        params.append(start_date.isoformat())
    if end_date:
        where_clauses.append(f"attendance.date <= {placeholder}")
        params.append(end_date.isoformat())

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    query = f"""
        SELECT attendance.id AS attendance_id, attendance.date, attendance.status, attendance.note,
               students.id AS student_id, students.name AS student_name, students.enrollment,
               subjects.id AS subject_id, subjects.name AS subject_name,
                             branches.id AS branch_id, COALESCE(attendance.branch_section, branches.name) AS branch_name
        FROM attendance
        JOIN students ON attendance.student_id = students.id
        LEFT JOIN subjects ON attendance.subject_id = subjects.id
        LEFT JOIN branches ON attendance.branch_id = branches.id
        {where_sql}
        ORDER BY attendance.date DESC
    """

    # Allow caller to append a LIMIT by passing max_rows through params via special key
    rows = db.execute(query, tuple(params)).fetchall()
    return rows


@app.route("/reports", methods=["GET"])
@login_required
@admin_required
def reports_index():
    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        subjects = db.execute(f"SELECT id, name FROM subjects ORDER BY name").fetchall()
        branches = db.execute(f"SELECT id, name FROM branches ORDER BY name").fetchall()

        # Parse and validate filters from query params
        subject_id_raw = request.args.get("subject_id") or None
        branch_id_raw = request.args.get("branch_id") or None
        subject_id = _parse_int_param(subject_id_raw)
        branch_id = _parse_int_param(branch_id_raw)
        start_date = _parse_date_param(request.args.get("start_date"))
        end_date = _parse_date_param(request.args.get("end_date"))

        # Validate date range
        if start_date and end_date and start_date > end_date:
            flash("Start date cannot be after end date.", "error")
            return redirect(url_for("reports_index"))

        # Validate provided subject/branch IDs exist
        if subject_id_raw and subject_id is None:
            flash("Invalid subject id.", "error")
            return redirect(url_for("reports_index"))
        if branch_id_raw and branch_id is None:
            flash("Invalid branch id.", "error")
            return redirect(url_for("reports_index"))

        if subject_id and not _exists_id(db, "subjects", subject_id):
            flash("Subject not found.", "error")
            return redirect(url_for("reports_index"))
        if branch_id and not _exists_id(db, "branches", branch_id):
            flash("Branch not found.", "error")
            return redirect(url_for("reports_index"))

        rows = None
        if request.args.get("preview"):
            # small preview limit for UI responsiveness
            rows = _get_report_rows(db, subject_id=subject_id, branch_id=branch_id, start_date=start_date, end_date=end_date)[:200]

        return render_template("reports_index.html", subjects=subjects, branches=branches, rows=rows, filters={"subject_id":(subject_id_raw or ""),"branch_id":(branch_id_raw or ""),"start_date":request.args.get("start_date"),"end_date":request.args.get("end_date")})
    except Exception as e:
        print(f"[reports_index] ERROR: {repr(e)}")
        flash("Reports are temporarily unavailable.", "error")
        return redirect(url_for("dashboard"))
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


def _rows_to_dataframe(rows):
    import pandas as _pd
    data = []
    for r in rows:
        data.append({
            "attendance_id": row_get(r, "attendance_id"),
            "date": row_get(r, "date"),
            "status": row_get(r, "status"),
            "note": row_get(r, "note"),
            "student_id": row_get(r, "student_id"),
            "student_name": row_get(r, "student_name"),
            "enrollment": row_get(r, "enrollment"),
            "subject_id": row_get(r, "subject_id"),
            "subject_name": row_get(r, "subject_name"),
            "branch_id": row_get(r, "branch_id"),
            "branch_name": row_get(r, "branch_name"),
        })
    return _pd.DataFrame(data)


def _register_timetable_routes_and_log():
    """Register timetable routes at import time and emit startup diagnostics."""
    try:
        import timetable as _timetable
    except Exception as e:
        print(f"[timetable] Timetable module unavailable: {type(e).__name__}: {str(e)}")
        return False

    try:
        _timetable.register_routes(app, get_db)
        print("[timetable] Timetable routes loaded")
    except Exception as e:
        print(f"[timetable] Failed to register timetable routes: {type(e).__name__}: {str(e)}")
        return False

    route_lines = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: (r.rule, r.endpoint)):
        methods = ",".join(sorted(m for m in rule.methods if m not in {"HEAD", "OPTIONS"}))
        route_lines.append(f"[routes] {rule.rule} -> {rule.endpoint} [{methods}]")

    print(f"[routes] Total registered Flask routes: {len(route_lines)}")
    for line in route_lines:
        print(line)

    try:
        db = get_db()
        try:
            _timetable.ensure_timetable_tables(db)
            row = db.execute("SELECT COUNT(1) AS c FROM timetable_slots").fetchone()
            count = row_get(row, "c", 0)
            print("[timetable] Timetable tables verified")
            print(f"[timetable] Timetable schema initialized: timetable_slots_count={count}")
            # Check normalized entries if possible
            try:
                row2 = db.execute("SELECT COUNT(1) AS c FROM timetable_entries").fetchone()
                count2 = row_get(row2, "c", 0)
                print(f"[timetable] Timetable entries detected: {count2}")
            except Exception:
                # ignore absence of normalized table
                pass
            # Postgres compatibility note
            try:
                is_pg = False
                if hasattr(db, "_conn"):
                    is_pg = "psycopg2" in type(db._conn).__module__
                elif type(db).__name__ == "_PostgresDB":
                    is_pg = True
                if is_pg:
                    print("[timetable] PostgreSQL timetable compatibility OK")
            except Exception:
                pass
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception as e:
        print(f"[timetable] Failed to verify timetable tables: {type(e).__name__}: {str(e)}")

    return True


# Register timetable routes at import time so template `url_for` lookups
# for `timetable_home` work under WSGI and other non-__main__ runtimes.
try:
    _register_timetable_routes_and_log()
except Exception as _t_err:
    print(f"[timetable] Auto-registration skipped: {type(_t_err).__name__}: {_t_err}")


@app.route("/reports/export.xlsx")
@login_required
@admin_required
def reports_export_xlsx():
    db = None
    try:
        db = get_db()
        # Validate params
        subject_id_raw = request.args.get("subject_id") or None
        branch_id_raw = request.args.get("branch_id") or None
        subject_id = _parse_int_param(subject_id_raw)
        branch_id = _parse_int_param(branch_id_raw)
        start_date = _parse_date_param(request.args.get("start_date"))
        end_date = _parse_date_param(request.args.get("end_date"))

        if start_date and end_date and start_date > end_date:
            flash("Start date cannot be after end date.", "error")
            return redirect(url_for("reports_index"))

        if subject_id_raw and subject_id is None:
            flash("Invalid subject id.", "error")
            return redirect(url_for("reports_index"))
        if branch_id_raw and branch_id is None:
            flash("Invalid branch id.", "error")
            return redirect(url_for("reports_index"))

        if subject_id and not _exists_id(db, "subjects", subject_id):
            flash("Subject not found.", "error")
            return redirect(url_for("reports_index"))
        if branch_id and not _exists_id(db, "branches", branch_id):
            flash("Branch not found.", "error")
            return redirect(url_for("reports_index"))

        # Enforce maximum export rows
        MAX_EXPORT_ROWS = int(os.environ.get("REPORT_MAX_ROWS", "10000"))
        rows = _get_report_rows(db, subject_id=subject_id, branch_id=branch_id, start_date=start_date, end_date=end_date)
        if len(rows) > MAX_EXPORT_ROWS:
            # Limit results to keep exports lightweight
            rows = rows[:MAX_EXPORT_ROWS]
            flash(f"Export limited to first {MAX_EXPORT_ROWS} rows.", "warning")
        df = _rows_to_dataframe(rows)

        bio = BytesIO()
        with pd.ExcelWriter(bio, engine="openpyxl") as writer:
            if df.empty:
                # create an empty sheet
                pd.DataFrame([{"info":"No records"}]).to_excel(writer, index=False, sheet_name="Report")
            else:
                df.to_excel(writer, index=False, sheet_name="Report")
        bio.seek(0)
        filename = f"attendance_report_{date.today().isoformat()}.xlsx"
        return send_file(bio, download_name=filename, as_attachment=True, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        print(f"[reports_export_xlsx] ERROR: {repr(e)}")
        flash("Export failed.", "error")
        return redirect(url_for("reports_index"))
    finally:
        if db:
            try: db.close()
            except: pass


@app.route("/reports/export.pdf")
@login_required
@admin_required
def reports_export_pdf():
    db = None
    try:
        db = get_db()
        # Validate params
        subject_id_raw = request.args.get("subject_id") or None
        branch_id_raw = request.args.get("branch_id") or None
        subject_id = _parse_int_param(subject_id_raw)
        branch_id = _parse_int_param(branch_id_raw)
        start_date = _parse_date_param(request.args.get("start_date"))
        end_date = _parse_date_param(request.args.get("end_date"))

        if start_date and end_date and start_date > end_date:
            flash("Start date cannot be after end date.", "error")
            return redirect(url_for("reports_index"))

        if subject_id_raw and subject_id is None:
            flash("Invalid subject id.", "error")
            return redirect(url_for("reports_index"))
        if branch_id_raw and branch_id is None:
            flash("Invalid branch id.", "error")
            return redirect(url_for("reports_index"))

        if subject_id and not _exists_id(db, "subjects", subject_id):
            flash("Subject not found.", "error")
            return redirect(url_for("reports_index"))
        if branch_id and not _exists_id(db, "branches", branch_id):
            flash("Branch not found.", "error")
            return redirect(url_for("reports_index"))

        # Enforce maximum export rows
        MAX_EXPORT_ROWS = int(os.environ.get("REPORT_MAX_ROWS", "10000"))
        rows = _get_report_rows(db, subject_id=subject_id, branch_id=branch_id, start_date=start_date, end_date=end_date)
        if len(rows) > MAX_EXPORT_ROWS:
            rows = rows[:MAX_EXPORT_ROWS]
            flash(f"Export limited to first {MAX_EXPORT_ROWS} rows.", "warning")
        df = _rows_to_dataframe(rows)

        bio = BytesIO()
        doc = SimpleDocTemplate(bio, pagesize=landscape(letter))
        elements = []

        # Build table data
        if df.empty:
            data = [["No records for the selected filters"]]
        else:
            cols = ["date", "status", "student_name", "enrollment", "subject_name", "branch_name", "note"]
            data = [cols]
            for _, row in df.iterrows():
                data.append([str(row.get(c, "")) for c in cols])

        table = Table(data)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
            ("GRID", (0,0), (-1,-1), 0.25, colors.black),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
        ]))
        elements.append(table)
        doc.build(elements)
        bio.seek(0)
        filename = f"attendance_report_{date.today().isoformat()}.pdf"
        return send_file(bio, download_name=filename, as_attachment=True, mimetype="application/pdf")
    except Exception as e:
        print(f"[reports_export_pdf] ERROR: {repr(e)}")
        flash("PDF export failed.", "error")
        return redirect(url_for("reports_index"))
    finally:
        if db:
            try: db.close()
            except: pass

if __name__ == "__main__":
    # Initialize database
    with app.app_context():
        get_db()
    
    # Register timetable routes
    try:
        from timetable import register_routes
        register_routes(app)
    except ImportError:
        print("[INFO] timetable module not found; skipping timetable routes")
    
    socketio.run(app, host="0.0.0.0", port=10000, debug=True)
