import os
import re
from datetime import date, timedelta, datetime
from io import BytesIO
import sys
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
logger = app.logger
# Email sending is handled by the `send_email` helper defined later in the file.

@app.context_processor
def inject_endpoint_helpers():
    """Expose a safe endpoint checker for templates."""
    return {
        "has_endpoint": lambda endpoint_name: endpoint_name in app.view_functions,
    }



def _safe_url_build_error_handler(error, endpoint, values):
    """Prevent template crashes when optional endpoints are unavailable.

    Returning a harmless target keeps pages renderable on production deployments
    even when some feature routes are not present in the loaded app module.
    """
    try:
        print(f"[WARN] Missing endpoint during url_for: endpoint={endpoint} values={values}")
    except Exception:
        # Never crash while trying to log
        pass

    return "#"


# TIMETABLE MODULE SAFE IMPORT: attempt to import now and log detailed errors.
_timetable = None
# Preserve the original import error for diagnostics
timetable_import_error = None
try:
    existing_endpoints = set(app.view_functions.keys())
    import timetable as _timetable
    new_endpoints = set(app.view_functions.keys()) - existing_endpoints
    print("TIMETABLE MODULE LOADED: new endpoints registered:", sorted(list(new_endpoints)))
    print("TIMETABLE BLUEPRINT LOADED")
    # Verify expected timetable endpoints
    expected = [
        "/timetable",
        "/timetable/manage",
        "/api/timetable-subjects",
        "/api/timetable-slots",
        "/api/current-period",
        "/api/attendance-periods",
    ]
    registered_paths = {rule.rule for rule in app.url_map.iter_rules()}
    for ep in expected:
        registered = ep in registered_paths
        print(f"TIMETABLE ROUTE CHECK: {ep} registered={registered}")
    print("TIMETABLE DB CHECK SKIPPED: database configuration initializes later")
except Exception as imp_err:
    _timetable = None
    timetable_import_error = imp_err
    print("[TIMETABLE LOAD ERROR] Could not import timetable module:", repr(imp_err))
    exc_type, exc_value, exc_tb = sys.exc_info()
    try:
        tb_last = traceback.extract_tb(exc_tb)[-1]
        print(f"[TIMETABLE LOAD ERROR] exception_type={exc_type.__name__} message={exc_value} file={tb_last.filename} line={tb_last.lineno} in {tb_last.name}")
    except Exception:
        pass
    traceback.print_exc()
# Flag for other code
TIMETABLE_AVAILABLE = bool(_timetable)
print(f"TIMETABLE_AVAILABLE={TIMETABLE_AVAILABLE}")
_ACADEMIC_DEPARTMENT_CODES = set(getattr(_timetable, "_ACADEMIC_DEPARTMENT_CODES", set()))


def safe_api(f):
    """Decorator for API endpoints to ensure errors return JSON and are logged."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            print("[API ERROR]", repr(e))
            traceback.print_exc()
            try:
                return jsonify({"error": str(e)}), 500
            except Exception:
                # Fallback plain text
                return ("{\"error\": \"Internal Server Error\"}", 500, {"Content-Type": "application/json"})
    return wrapper

# Use a stable SQLite file path relative to the application folder unless a PostgreSQL URL is provided.
db_env = os.environ.get("DATABASE_URL")
if db_env:
    database_path = db_env
else:
    # Always use absolute path relative to app.py location
    app_dir = os.path.dirname(os.path.abspath(__file__))
    shared_db_path = os.path.abspath(os.path.join(app_dir, "..", "attendance.db"))
    local_db_path = os.path.abspath(os.path.join(app_dir, "attendance.db"))
    database_path = shared_db_path if os.path.exists(shared_db_path) else local_db_path
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

def _normalize_db_url(url: str) -> str:
    """Normalize a PostgreSQL connection URL for psycopg2 + Neon compatibility.

    - Converts legacy postgres:// scheme to postgresql://
    - Ensures sslmode=require is present (required by Neon and most managed PG providers)
    """
    if not url:
        return url
    # psycopg2 requires postgresql://, not postgres://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    # Inject sslmode=require if missing (Neon mandates SSL)
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = url + sep + "sslmode=require"
    elif "sslmode=disable" in url or "sslmode=prefer" in url:
        import re as _re
        url = _re.sub(r"sslmode=[a-zA-Z0-9_-]+", "sslmode=require", url)
    return url


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

        class _PostgresDB:
            def __init__(self, conn):
                self._conn = conn
            def execute(self, query, params=()):
                try:
                    cur = self._conn.cursor(cursor_factory=DictCursor)
                    cur.execute(query, params)
                    return cur
                except Exception as exc:
                    # Auto-rollback on InFailedSqlTransaction so subsequent queries don't cascade-fail
                    if "InFailedSqlTransaction" in type(exc).__name__ or "current transaction is aborted" in str(exc):
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        cur = self._conn.cursor(cursor_factory=DictCursor)
                        cur.execute(query, params)
                        return cur
                    raise
            def commit(self): return self._conn.commit()
            def rollback(self): return self._conn.rollback()
            def close(self): return self._conn.close()

        # Normalize URL for Neon / any managed PostgreSQL provider
        db_url = _normalize_db_url(db_url)

        try:
            from urllib.parse import urlparse as _urlparse
            _parsed = _urlparse(db_url)
            parsed_host = _parsed.hostname
            parsed_db   = _parsed.path.lstrip("/")
            parsed_user = _parsed.username
            # Detect provider from hostname
            provider = "Neon" if parsed_host and "neon.tech" in parsed_host else \
                       "Supabase" if parsed_host and "supabase.co" in parsed_host else \
                       "PostgreSQL"
            print(f"[DB] Provider: {provider}")
            print("[DB HOST]", parsed_host)
            print("[DB NAME]", parsed_db)
            print("[DB USER]", parsed_user)
        except Exception:
            pass

        try:
            print("[DB] Connecting to PostgreSQL (Neon)...")
            conn = psycopg2.connect(
                db_url,
                connect_timeout=15,
            )
            conn.set_session(autocommit=False)
            db = _PostgresDB(conn)
            print("[DB] PostgreSQL connected successfully")
        except psycopg2.OperationalError as e:
            print("[DB ERROR]", str(e))
            import traceback as _tb
            _tb.print_exc()
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


def _coerce_int(v):
    try:
        if v is None or v == "":
            return None
        return int(v)
    except Exception:
        return None


def _fast_student_hash(password: str) -> str:
    """Light-weight password hash for bulk student import.

    Werkzeug's default generate_password_hash() uses pbkdf2:sha256 with
    260,000 iterations — about 0.3-0.5 s per call.  For a 300-student CSV
    that is 90-150 s of pure CPU, which exceeds Gunicorn's worker timeout.

    Student passwords are only 4-digit PINs (very low entropy), so the extra
    iterations add no meaningful security.  10,000 rounds of pbkdf2:sha256 is
    still NIST-compliant for low-entropy secrets and runs in ~2 ms.

    check_password_hash() understands this format without any changes.
    """
    return generate_password_hash(password, method="pbkdf2:sha256:10000", salt_length=8)


def _normalize_lookup_key(value):
    text = "" if value is None else str(value)
    return "".join(ch.lower() for ch in text.strip() if ch.isalnum())


def normalize_text(value):
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = text.replace("-", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text



# Advanced alias map for common short forms
SUBJECT_ALIASES = {
    'bee': ['basic electrical engineering', 'basic electricals', 'basic electrical', 'bee'],
    'odevc': ['ordinary differential equations and vector calculus', 'ordinary differential equations', 'vector calculus', 'odevc'],
    'ds': ['data structures', 'data structure', 'ds'],
    'physics': ['advanced engineering physics', 'physics'],
    'chemistry': ['engineering chemistry', 'chemistry'],
}


DEFAULT_SUBJECT_ALIAS_ROWS = [
    ("BEE", "Basic Electrical Engineering"),
    ("ODEVC", "Ordinary Differential Equations and Vector Calculus"),
    ("DS", "Data Structures"),
    ("PHYSICS", "Advanced Engineering Physics"),
    ("CHEMISTRY", "Engineering Chemistry"),
]


def _ensure_subject_alias_table(db):
    placeholder = get_placeholder()
    try:
        db.execute(
            f"""
            CREATE TABLE IF NOT EXISTS subject_aliases (
                id {'SERIAL PRIMARY KEY' if str(app.config.get('DATABASE', '')).startswith('postgres') else 'INTEGER PRIMARY KEY AUTOINCREMENT'},
                alias TEXT UNIQUE NOT NULL,
                canonical_name TEXT NOT NULL
            )
            """
        )
        for alias, canonical_name in DEFAULT_SUBJECT_ALIAS_ROWS:
            if str(app.config.get("DATABASE", "")).startswith("postgres"):
                db.execute(
                    f"INSERT INTO subject_aliases (alias, canonical_name) VALUES ({placeholder}, {placeholder}) ON CONFLICT (alias) DO UPDATE SET canonical_name = EXCLUDED.canonical_name",
                    (alias, canonical_name),
                )
            else:
                db.execute(
                    f"INSERT OR REPLACE INTO subject_aliases (alias, canonical_name) VALUES ({placeholder}, {placeholder})",
                    (alias, canonical_name),
                )
        try:
            db.commit()
        except Exception:
            pass
    except Exception:
        print("[schema] subject_aliases table initialization failed")


def _load_subject_alias_map(db):
    alias_map = {key: list(values) for key, values in SUBJECT_ALIASES.items()}
    try:
        rows = db.execute("SELECT alias, canonical_name FROM subject_aliases").fetchall()
    except Exception:
        rows = []
    for row in rows:
        alias = _normalize_lookup_key(row_get(row, "alias"))
        canonical = str(row_get(row, "canonical_name") or "").strip()
        if not alias or not canonical:
            continue
        alias_map.setdefault(alias, [])
        if canonical not in alias_map[alias]:
            alias_map[alias].append(canonical)
    return alias_map


def _ensure_timetable_entry_text_columns(db):
    try:
        cols = {name.lower() for name in _table_columns(db, "timetable_entries")}
    except Exception:
        cols = set()
    if not cols:
        return
    try:
        if "subject_name" not in cols:
            db.execute("ALTER TABLE timetable_entries ADD COLUMN subject_name TEXT")
        if "faculty_name" not in cols:
            db.execute("ALTER TABLE timetable_entries ADD COLUMN faculty_name TEXT")
        try:
            db.commit()
        except Exception:
            pass
    except Exception:
        print("[schema] timetable_entries fallback column initialization skipped")


def split_branch_section(value):
    """Split a combined branch-section string into (branch, section).

    Accepts formats like 'CSE-A', 'CSE A', 'CSEA', 'cse-a', 'cse a'.
    Returns tuple (branch, section) where section may be empty string.
    """
    if not value:
        return "", ""
    v = str(value).strip().upper()
    if v in _ACADEMIC_DEPARTMENT_CODES:
        return v, ""
    # Split by hyphen, slash, or space
    parts = re.split(r"[-/ ]+", v)
    if len(parts) >= 2:
        return parts[0], parts[-1]
    # Check if name is like CSEA or CSMB
    for code in sorted(_ACADEMIC_DEPARTMENT_CODES, key=len, reverse=True):
        if v.startswith(code) and len(v) > len(code):
            suffix = v[len(code):].strip("- _/")
            if suffix and re.fullmatch(r"[A-Z0-9]{1,4}", suffix):
                return code, suffix
    m = re.match(r"^([A-Z]{2,5})([A-Z0-9]{1,4})$", v)
    if m and m.group(1) not in _ACADEMIC_DEPARTMENT_CODES:
        return m.group(1), m.group(2)
    return v, ""


def section_matches(entry_sec, req_sec):
    if not req_sec:
        return True
    e_norm = normalize_text(entry_sec)
    r_norm = normalize_text(req_sec)
    if e_norm == r_norm:
        return True
    e_br, e_s = split_branch_section(entry_sec)
    r_br, r_s = split_branch_section(req_sec)
    if e_s and r_s and normalize_text(e_s) == normalize_text(r_s):
        return True
    if e_s and normalize_text(e_s) == r_norm:
        return True
    if r_s and normalize_text(r_s) == e_norm:
        return True
    return False


def _normalize_attendance_section_input(value):
    """Normalize a teacher-provided section string for attendance lookups.

    Returns an empty string when no clear section can be derived.
    """
    if not value:
        return ""
    v = str(value).strip()
    if not v:
        return ""
    # If value is just a department code, treat as no explicit section
    if v.upper() in _ACADEMIC_DEPARTMENT_CODES:
        return ""
    # Use split_branch_section to extract suffix if present
    base, sec = split_branch_section(v)
    if sec:
        return sec.strip()
    # Otherwise return the trimmed token
    return v.strip()

def day_matches(d1, d2):
    if not d1 or not d2:
        return False
    return normalize_text(d1)[:3] == normalize_text(d2)[:3]

def subject_name_matches(sub1, sub2):
    n1 = normalize_text(sub1)
    n2 = normalize_text(sub2)
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    # Resolve aliases
    def get_canonicals(name):
        norm = normalize_text(name)
        canonicals = {norm}
        for alias, full_names in SUBJECT_ALIASES.items():
            norm_alias = normalize_text(alias)
            norm_fulls = [normalize_text(f) for f in full_names]
            if norm == norm_alias or norm in norm_fulls:
                canonicals.add(norm_alias)
                for f in norm_fulls:
                    canonicals.add(f)
        return canonicals
    c1 = get_canonicals(sub1)
    c2 = get_canonicals(sub2)
    return bool(c1 & c2)

def subject_matches(entry_subject_id, entry_subject_name, req_subject_id, req_subject_name):
    if not req_subject_id and not req_subject_name:
        return True
    if req_subject_id and entry_subject_id and str(req_subject_id) == str(entry_subject_id):
        return True
    if subject_name_matches(entry_subject_name, req_subject_name):
        return True
    return False

def get_subject_display_name(name):
    if not name:
        return ""
    norm = normalize_text(name)
    for alias, full_names in SUBJECT_ALIASES.items():
        norm_alias = normalize_text(alias)
        norm_fulls = [normalize_text(f) for f in full_names]
        if norm == norm_alias or norm in norm_fulls:
            return alias.upper()
    return name.title()


def _resolve_timetable_branch_lookup(db, branch_id, section=""):
    placeholder = get_placeholder()
    selected_branch_id = None
    selected_branch_name = ""
    section_val = (section or "").strip()

    branch_id_val = _coerce_int(branch_id)
    if branch_id_val is not None:
        row = db.execute(f"SELECT id, name FROM branches WHERE id = {placeholder}", (branch_id_val,)).fetchone()
        if row:
            selected_branch_id = row_get(row, "id")
            selected_branch_name = (row_get(row, "name") or "").strip()

    if not selected_branch_id and branch_id:
        raw_name = str(branch_id).strip()
        row = db.execute(f"SELECT id, name FROM branches WHERE UPPER(name) = {placeholder}", (raw_name.upper(),)).fetchone()
        if not row:
            base_branch, derived_section = split_branch_section(raw_name)
            if derived_section and not section_val:
                section_val = derived_section
            if base_branch and base_branch.upper() != raw_name.upper():
                row = db.execute(f"SELECT id, name FROM branches WHERE UPPER(name) = {placeholder}", (base_branch.upper(),)).fetchone()
        if row:
            selected_branch_id = row_get(row, "id")
            selected_branch_name = (row_get(row, "name") or "").strip()

    if selected_branch_name:
        base_branch, derived_section = split_branch_section(selected_branch_name)
        if base_branch and base_branch.upper() != selected_branch_name.upper():
            row = db.execute(f"SELECT id, name FROM branches WHERE UPPER(name) = {placeholder}", (base_branch.upper(),)).fetchone()
            if row:
                selected_branch_id = row_get(row, "id")
                selected_branch_name = (row_get(row, "name") or "").strip()
                if not section_val:
                    section_val = derived_section

    return selected_branch_id, selected_branch_name, section_val


def _get_timetable_subjects_for_branch(db, branch_id, section=None, weekday=None, weekday_short=None):
    placeholder = get_placeholder()
    selected_branch_id, selected_branch_name, section_val = _resolve_timetable_branch_lookup(db, branch_id, section=section)

    if selected_branch_id is None:
        print(f"[attendance] selected branch_id={branch_id} section={section_val or ''} weekday={weekday or ''} rows=0 subjects=0")
        return []

    params = [selected_branch_id]
    sql = (
        "SELECT DISTINCT TRIM(COALESCE(subject_name, '')) AS subject_name, "
        "MIN(subject_id) AS subject_id, MIN(start_time) AS first_start "
        f"FROM timetable_entries WHERE branch_id = {placeholder}"
    )
    if section_val:
        combined_sec = f"{selected_branch_name}-{section_val}"
        sql += f" AND (LOWER(TRIM(COALESCE(section, ''))) = LOWER(TRIM({placeholder})) OR LOWER(TRIM(COALESCE(section, ''))) = LOWER(TRIM({placeholder})))"
        params.extend([section_val, combined_sec])
    if weekday:
        weekday_short = weekday_short or str(weekday)[:3]
        sql += f" AND (LOWER(TRIM(COALESCE(day, ''))) = LOWER(TRIM({placeholder})) OR LOWER(TRIM(COALESCE(day, ''))) = LOWER(TRIM({placeholder})))"
        params.extend([weekday, weekday_short])
    sql += " AND COALESCE(TRIM(subject_name), '') <> '' GROUP BY LOWER(TRIM(subject_name)), TRIM(COALESCE(subject_name, ''))"
    if weekday:
        sql += " ORDER BY first_start, subject_name"
    else:
        sql += " ORDER BY subject_name"

    try:
        rows = db.execute(sql, tuple(params)).fetchall()
    except Exception as e:
        print(f"[attendance] timetable subject lookup failed: {repr(e)}")
        rows = []

    subjects = []
    seen = set()
    for row in rows:
        subject_name = (row_get(row, "subject_name") or "").strip()
        if not subject_name:
            continue
        key = normalize_text(subject_name)
        if key in seen:
            continue
        seen.add(key)
        subject_id_value = row_get(row, "subject_id")
        subjects.append({"id": subject_id_value if subject_id_value is not None else subject_name, "name": subject_name, "canonical": subject_name})

    print(
        f"[attendance] selected branch_id={selected_branch_id} section={section_val or ''} weekday={weekday or ''} rows={len(rows)} subjects={len(subjects)}"
    )
    return subjects


def _get_timetable_sections_for_branch(db, branch_id):
    placeholder = get_placeholder()
    selected_branch_id, selected_branch_name, _ = _resolve_timetable_branch_lookup(db, branch_id, section="")

    if selected_branch_id is None:
        return []

    rows = db.execute(
        f"SELECT DISTINCT section FROM timetable_entries WHERE branch_id = {placeholder} AND COALESCE(TRIM(section), '') <> '' ORDER BY section",
        (selected_branch_id,)
    ).fetchall()
    sections = []
    seen = set()
    prefix = (selected_branch_name or "").upper() + "-"
    for row in rows:
        section_val = (row_get(row, "section") or "").strip()
        if not section_val:
            continue
        norm_sec = section_val
        if norm_sec.upper().startswith(prefix):
            norm_sec = norm_sec[len(prefix):].strip()
        norm_key = norm_sec.lower()
        if norm_key not in seen:
            seen.add(norm_key)
            sections.append(norm_sec)
    sections.sort()
    return sections


def _attendance_no_schedule_reason(db, branch_id, section="", weekday=""):
    placeholder = get_placeholder()
    try:
        total_row = db.execute("SELECT COUNT(*) AS c FROM timetable_entries").fetchone()
        total_count = int(row_get(total_row, "c", 0) or 0)
    except Exception:
        total_count = 0
    if total_count == 0:
        return "No timetable configured"

    selected_branch_id, _, section_val = _resolve_timetable_branch_lookup(db, branch_id, section=section)
    if selected_branch_id is None:
        return "Branch mismatch"

    try:
        branch_row = db.execute(
            f"SELECT COUNT(*) AS c FROM timetable_entries WHERE branch_id = {placeholder}",
            (selected_branch_id,),
        ).fetchone()
        branch_count = int(row_get(branch_row, "c", 0) or 0)
    except Exception:
        branch_count = 0
    if branch_count == 0:
        return "Branch mismatch"

    if section_val:
        _, selected_branch_name, _ = _resolve_timetable_branch_lookup(db, selected_branch_id)
        combined_sec = f"{selected_branch_name}-{section_val}"
        try:
            sec_row = db.execute(
                f"SELECT COUNT(*) AS c FROM timetable_entries WHERE branch_id = {placeholder} AND (LOWER(TRIM(COALESCE(section, ''))) = LOWER(TRIM({placeholder})) OR LOWER(TRIM(COALESCE(section, ''))) = LOWER(TRIM({placeholder})))",
                (selected_branch_id, section_val, combined_sec),
            ).fetchone()
            sec_count = int(row_get(sec_row, "c", 0) or 0)
        except Exception:
            sec_count = 0
        if sec_count == 0:
            return "Section mismatch"

    if weekday:
        weekday_short = str(weekday)[:3]
        try:
            day_sql = (
                f"SELECT COUNT(*) AS c FROM timetable_entries WHERE branch_id = {placeholder} "
                f"AND (LOWER(TRIM(COALESCE(day, ''))) = LOWER(TRIM({placeholder})) OR LOWER(TRIM(COALESCE(day, ''))) = LOWER(TRIM({placeholder})))"
            )
            params = [selected_branch_id, weekday, weekday_short]
            if section_val:
                _, selected_branch_name, _ = _resolve_timetable_branch_lookup(db, selected_branch_id)
                combined_sec = f"{selected_branch_name}-{section_val}"
                day_sql += f" AND (LOWER(TRIM(COALESCE(section, ''))) = LOWER(TRIM({placeholder})) OR LOWER(TRIM(COALESCE(section, ''))) = LOWER(TRIM({placeholder})))"
                params.extend([section_val, combined_sec])
            day_row = db.execute(day_sql, tuple(params)).fetchone()
            day_count = int(row_get(day_row, "c", 0) or 0)
        except Exception:
            day_count = 0
        if day_count == 0:
            return "Weekday mismatch"

    return ""


def _token_similarity(a, b):
    """Return a simple token overlap similarity (0..1)."""
    if not a or not b:
        return 0.0
    ta = set(t.lower() for t in re.split(r"[^A-Za-z0-9]+", str(a)) if t)
    tb = set(t.lower() for t in re.split(r"[^A-Za-z0-9]+", str(b)) if t)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    return len(inter) / len(union)


_ACRONYM_STOPWORDS = {"and", "of", "the", "for", "with", "to", "in", "on", "at", "by", "from"}


def _build_acronym(tokens):
    letters = [token[0] for token in tokens if token and token.lower() not in _ACRONYM_STOPWORDS and token[0].isalnum()]
    return "".join(letters).lower()


def _text_variants(value):
    cleaned = "" if value is None else str(value).strip()
    normalized = _normalize_lookup_key(cleaned)
    tokens = [token for token in re.split(r"[^A-Za-z0-9]+", cleaned) if token]
    variants = {cleaned.lower(), normalized}
    if tokens:
        variants.add("".join(tokens).lower())
        acronym = _build_acronym(tokens)
        if acronym:
            variants.add(acronym.lower())
        variants.update(token.lower() for token in tokens)
        variants.add(tokens[-1].lower())
    return {variant for variant in variants if variant}


def _subject_variants(value):
    cleaned = "" if value is None else str(value).strip()
    variants = set(_text_variants(cleaned))
    if cleaned:
        letters = [ch for ch in cleaned if ch.isalpha()]
        if letters and cleaned.replace(" ", "").isalpha():
            variants.add("".join(letters).lower())
    return variants


def _section_variants(value):
    cleaned = "" if value is None else str(value).strip()
    variants = {cleaned.lower(), _normalize_lookup_key(cleaned)}
    parts = [part.strip() for part in re.split(r"[^A-Za-z0-9]+", cleaned) if part.strip()]
    if parts:
        variants.add(parts[-1].lower())
        variants.add(_normalize_lookup_key(parts[-1]))
        if len(parts) == 1:
            variants.update(_text_variants(parts[0]))
    return {variant for variant in variants if variant}


def _day_variants(value):
    cleaned = "" if value is None else str(value).strip()
    normalized = _normalize_lookup_key(cleaned)
    variants = {cleaned.lower(), normalized}
    if normalized:
        variants.add(normalized[:3])
    day_aliases = {
        "mon": "monday",
        "monday": "monday",
        "tue": "tuesday",
        "tues": "tuesday",
        "tuesday": "tuesday",
        "wed": "wednesday",
        "wednesday": "wednesday",
        "thu": "thursday",
        "thur": "thursday",
        "thurs": "thursday",
        "thursday": "thursday",
        "fri": "friday",
        "friday": "friday",
        "sat": "saturday",
        "saturday": "saturday",
        "sun": "sunday",
        "sunday": "sunday",
    }
    if normalized in day_aliases:
        variants.add(day_aliases[normalized])
    if normalized[:3] in day_aliases:
        variants.add(day_aliases[normalized[:3]])
    return {variant for variant in variants if variant}


def _variants_match(left, right, variant_builder=_text_variants):
    left_variants = variant_builder(left)
    right_variants = variant_builder(right)
    if left_variants & right_variants:
        return True
    left_key = _normalize_lookup_key(left)
    right_key = _normalize_lookup_key(right)
    if left_key and right_key:
        if left_key in right_key or right_key in left_key:
            return True
    return False


def _subject_matches(left, right):
    return _variants_match(left, right, _subject_variants)


def _section_matches(left, right):
    return _variants_match(left, right, _section_variants)


def _day_matches(left, right):
    return _variants_match(left, right, _day_variants)


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


def _dashboard_default_context():
    render_env = bool(os.environ.get("RENDER") or os.environ.get("RENDER_INTERNAL_HOSTNAME"))
    return {
        "branch_count": 0,
        "student_count": 0,
        "subject_count": 0,
        "attendance_count": 0,
        "total_classes": 0,
        "present_count": 0,
        "absent_count": 0,
        "overall_percentage": 0,
        "today_percentage": 0,
        "active_classes_today": 0,
        "low_attendance_alerts": 0,
        "total_teachers": 0,
        "total_semesters": 0,
        "subject_data": [],
        "subject_chart_labels": [],
        "subject_chart_percentages": [],
        "branch_data": [],
        "chart_data": [],
        "trend_labels": [],
        "trend_percentages": [],
        "monthly_labels": [],
        "monthly_percentages": [],
        "current_active_period": None,
        "upcoming_timetable": [],
        "recent_activity": [],
        "database_info": (lambda _url=str(app.config.get("DATABASE", "")): {
            "storage": "Neon PostgreSQL" if "neon.tech" in _url else
                       "Supabase PostgreSQL" if "supabase.co" in _url else
                       "PostgreSQL" if _url.startswith("postgres") else "SQLite",
            "path": _url,
        })(),
        "mail_info": {
            "configured": is_mail_configured(),
            "server": app.config.get("MAIL_SERVER"),
            "port": app.config.get("MAIL_PORT"),
            "username": app.config.get("MAIL_USERNAME"),
            "tls": app.config.get("MAIL_USE_TLS"),
            "render_env": render_env,
        },
        "persistence_warning": render_env and not str(app.config.get("DATABASE", "")).startswith("postgres"),  # Neon = postgres URL = no warning
        "error_mode": False,
    }


def _table_columns(db, table_name):
    """Return a set of columns for a table, or an empty set if the table is missing."""
    try:
        if str(app.config.get("DATABASE", "")).startswith("postgres"):
            rows = db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
                (table_name,),
            ).fetchall()
            return {row_get(row, "column_name") for row in rows if row_get(row, "column_name")}
        rows = db.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {row_get(row, "name") for row in rows if row_get(row, "name")}
    except Exception:
        return set()


def _safe_fetchone_value(row, default=0):
    if row is None:
        return default
    try:
        if isinstance(row, tuple):
            return row[0] if row else default
        try:
            keys = list(row.keys())
            if keys:
                return row_get(row, keys[0], default)
        except Exception:
            pass
        return row[0] if row else default
    except Exception:
        return default


def _log_db_error(scope, sql, params=(), db=None):
    try:
        app.logger.exception("[%s] database query failed", scope)
    except Exception:
        pass
    print(f"[{scope}] failing SQL: {sql}")
    print(f"[{scope}] parameters: {params}")
    print(f"[{scope}] traceback:\n{traceback.format_exc()}")
    if db is not None:
        try:
            schema_state = verify_database_schema(db)
            print(f"[{scope}] missing tables: {schema_state.get('missing_tables', [])}")
            print(f"[{scope}] missing columns: {schema_state.get('missing_columns', {})}")
        except Exception:
            print(f"[{scope}] schema verification failed:\n{traceback.format_exc()}")


def verify_database_schema(db=None):
    created_here = False
    if db is None:
        db = get_db()
        created_here = True
    required = {
        "branches": {"id", "name"},
        "students": {"id", "name", "enrollment", "branch_id"},
        "subjects": {"id", "name", "branch_id"},
        "attendance": {"id", "student_id", "branch_id", "branch_section", "section", "subject_id", "subject_name", "period", "date", "status", "teacher_id", "marked_at"},
        "teachers": {"id", "name", "username", "password", "subject_id", "branch_id"},
        "teacher_branches": {"id", "teacher_id", "branch_id"},
        "teacher_subjects": {"id", "teacher_id", "subject_id"},
        "settings": {"id", "key", "value"},
        "timetable_entries": {"id", "branch_id", "section", "subject_name", "faculty_name", "start_time", "end_time"},
    }
    missing_tables = []
    missing_columns = {}
    try:
        for table_name, required_columns in required.items():
            cols = _table_columns(db, table_name)
            if not cols:
                missing_tables.append(table_name)
                continue
            missing = sorted(required_columns - cols)
            if missing:
                missing_columns[table_name] = missing
        if missing_tables or missing_columns:
            print(f"[schema] missing tables={missing_tables} missing_columns={missing_columns}")
        return {"missing_tables": missing_tables, "missing_columns": missing_columns}
    finally:
        if created_here:
            try:
                db.close()
            except Exception:
                pass


def _validate_dashboard_schema(db):
    """Log missing dashboard-critical tables/columns without stopping startup."""
    return verify_database_schema(db)


def _ensure_column(db, table_name, column_name, column_definition):
    try:
        cols = _table_columns(db, table_name)
        if cols and column_name not in cols:
            db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")
    except Exception:
        print(f"[schema] failed to ensure {table_name}.{column_name}:\n{traceback.format_exc()}")


def _ensure_teacher_schema(db):
    placeholder = get_placeholder()
    if str(app.config.get("DATABASE", "")).startswith("postgres"):
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS teachers (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                subject_id INTEGER,
                branch_id INTEGER,
                subject_name TEXT,
                email TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS teacher_branches (
                id SERIAL PRIMARY KEY,
                teacher_id INTEGER NOT NULL,
                branch_id INTEGER NOT NULL,
                UNIQUE(teacher_id, branch_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS teacher_subjects (
                id SERIAL PRIMARY KEY,
                teacher_id INTEGER NOT NULL,
                subject_id INTEGER NOT NULL,
                UNIQUE(teacher_id, subject_id)
            )
            """
        )
    else:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS teachers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                subject_id INTEGER,
                branch_id INTEGER,
                subject_name TEXT,
                email TEXT
            );

            CREATE TABLE IF NOT EXISTS teacher_branches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_id INTEGER NOT NULL,
                branch_id INTEGER NOT NULL,
                UNIQUE(teacher_id, branch_id)
            );

            CREATE TABLE IF NOT EXISTS teacher_subjects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_id INTEGER NOT NULL,
                subject_id INTEGER NOT NULL,
                UNIQUE(teacher_id, subject_id)
            );
            """
        )

    _ensure_column(db, "teachers", "name", "TEXT")
    _ensure_column(db, "teachers", "username", "TEXT")
    _ensure_column(db, "teachers", "password", "TEXT")
    _ensure_column(db, "teachers", "subject_id", "INTEGER")
    _ensure_column(db, "teachers", "branch_id", "INTEGER")
    _ensure_column(db, "teachers", "subject_name", "TEXT")
    _ensure_column(db, "teachers", "email", "TEXT")
    try:
        db.commit()
    except Exception:
        pass

    teacher = db.execute(
        f"SELECT id FROM teachers WHERE username = {placeholder}",
        ("teacher1",),
    ).fetchone()
    if not teacher:
        db.execute(
            f"INSERT INTO teachers (name, username, password, subject_id, branch_id, subject_name) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
            ("Teacher One", "teacher1", generate_password_hash("1234"), None, None, ""),
        )
        try:
            db.commit()
        except Exception:
            pass


def _ensure_teacher_support_schema(db):
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


def teacher_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("role") != "teacher" or not session.get("teacher_id"):
            flash("Please log in as a teacher to continue.", "warning")
            return redirect(url_for("teacher_login"))
        return f(*args, **kwargs)
    return decorated_function


def teacher_required(f):
    """Decorator to ensure the logged-in user is a teacher and teacher-specific session keys exist."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("role") != "teacher" or not session.get("teacher_id"):
            flash("Teacher access required.", "warning")
            return redirect(url_for("teacher_login"))
        return f(*args, **kwargs)
    return decorated_function


def _resolve_teacher_assignments(db, teacher_id):
    placeholder = get_placeholder()
    branches = []
    subjects = []
    try:
        branches = db.execute(
            f"""
            SELECT b.id, b.name, b.location, tb.branch_id
            FROM teacher_branches tb
            JOIN branches b ON b.id = tb.branch_id
            WHERE tb.teacher_id = {placeholder}
            ORDER BY b.name
            """,
            (teacher_id,),
        ).fetchall()
    except Exception:
        branches = []
    try:
        subjects = db.execute(
            f"""
            SELECT s.id, s.name, s.branch_id
            FROM teacher_subjects ts
            JOIN subjects s ON s.id = ts.subject_id
            WHERE ts.teacher_id = {placeholder}
            ORDER BY s.name
            """,
            (teacher_id,),
        ).fetchall()
    except Exception:
        subjects = []
    return branches, subjects


def get_teacher_context(db=None):
    if session.get("role") != "teacher":
        return None
    created_here = False
    if db is None:
        db = get_db()
        created_here = True
    try:
        teacher_id = session.get("teacher_id") or session.get("user_id")
        if not teacher_id:
            return None
        placeholder = get_placeholder()
        teacher = None
        try:
            teacher = db.execute(
                f"SELECT * FROM teachers WHERE id = {placeholder}",
                (teacher_id,),
            ).fetchone()
        except Exception:
            teacher = None
        if not teacher:
            try:
                teacher = db.execute(
                    f"SELECT id, username, username AS name, password, CAST(NULL AS INTEGER) AS subject_id, CAST(NULL AS INTEGER) AS branch_id, CAST(NULL AS TEXT) AS subject_name FROM users WHERE id = {placeholder} AND role = {placeholder}",
                    (teacher_id, "teacher"),
                ).fetchone()
            except Exception:
                teacher = None
        if not teacher:
            return None

        assigned_classes = _get_teacher_assignments(db, teacher_id)

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

        def _dedupe(rows, key_name):
            seen = set()
            unique_rows = []
            for item in rows:
                key = row_get(item, key_name)
                if key in seen:
                    continue
                seen.add(key)
                unique_rows.append(item)
            return unique_rows

        assigned_branches = _dedupe(assigned_branches, "id")
        assigned_subjects = _dedupe(assigned_subjects, "id")

        current_branch_id = session.get("teacher_branch_id") or row_get(teacher, "branch_id")
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
            branch_row = db.execute(
                f"SELECT id, name, location FROM branches WHERE id = {placeholder}",
                (current_branch_id,),
            ).fetchone()
            if branch_row:
                current_branch_name = row_get(branch_row, "name") or ""

        if not current_branch_id and assigned_branches:
            current_branch_id = row_get(assigned_branches[0], "id")
            current_branch_name = row_get(assigned_branches[0], "name") or ""
        if not current_subject_id and assigned_subjects:
            current_subject_id = row_get(assigned_subjects[0], "id")

        subject_name = row_get(teacher, "subject_name") or ""
        if current_subject_id:
            subject_row = db.execute(
                f"SELECT id, name, branch_id FROM subjects WHERE id = {placeholder}",
                (current_subject_id,),
            ).fetchone()
            if subject_row:
                subject_name = row_get(subject_row, "name") or subject_name

        teacher_name = row_get(teacher, "name") or row_get(teacher, "username") or session.get("username") or "Teacher"

        assigned_subject_ids = [str(row_get(item, "id")) for item in assigned_subjects if row_get(item, "id") is not None]
        assigned_branch_ids = [str(row_get(item, "id")) for item in assigned_branches if row_get(item, "id") is not None]
        assigned_sections = sorted({row_get(item, "section") for item in assigned_classes if row_get(item, "section")})
        assigned_semesters = sorted({row_get(item, "semester") for item in assigned_classes if row_get(item, "semester")})

        return {
            "teacher": {
                "id": row_get(teacher, "id"),
                "name": teacher_name,
                "username": row_get(teacher, "username") or session.get("username") or "",
                "subject_name": subject_name,
                "current_subject_id": current_subject_id,
                "current_branch_id": current_branch_id,
                "current_branch_name": current_branch_name,
                "current_section": current_section,
                "assigned_subjects": [
                    {
                        "id": row_get(subject, "id"),
                        "name": row_get(subject, "name"),
                        "branch_id": row_get(subject, "branch_id"),
                    }
                    for subject in assigned_subjects
                ],
                "assigned_branches": [
                    {
                        "id": row_get(branch, "id"),
                        "name": row_get(branch, "name"),
                        "location": row_get(branch, "location"),
                        "section": row_get(branch, "section") or "",
                    }
                    for branch in assigned_branches
                ],
                "assigned_subjects_count": len(assigned_subjects),
                "assigned_branches_count": len(assigned_branches),
                "assigned_classes": assigned_classes,
                "assigned_subject_ids": assigned_subject_ids,
                "assigned_branch_ids": assigned_branch_ids,
                "assigned_sections": assigned_sections,
                "assigned_semesters": assigned_semesters,
            },
            "teacher_id": row_get(teacher, "id"),
            "name": teacher_name,
            "username": row_get(teacher, "username") or session.get("username") or "",
            "subject_name": subject_name,
            "subject_id": current_subject_id,
            "current_subject_id": current_subject_id,
            "subject_row": None,
            "current_branch_id": current_branch_id,
            "current_branch_name": current_branch_name,
            "current_section": current_section,
            "assigned_branches": [
                {
                    "id": row_get(branch, "id"),
                    "name": row_get(branch, "name"),
                    "location": row_get(branch, "location"),
                    "section": row_get(branch, "section") or "",
                }
                for branch in assigned_branches
            ],
            "assigned_branches_count": len(assigned_branches),
            "assigned_subjects": [
                {
                    "id": row_get(subject, "id"),
                    "name": row_get(subject, "name"),
                    "branch_id": row_get(subject, "branch_id"),
                }
                for subject in assigned_subjects
            ],
            "assigned_subjects_count": len(assigned_subjects),
            "assigned_classes": assigned_classes,
            "assigned_subject_ids": assigned_subject_ids,
            "assigned_branch_ids": assigned_branch_ids,
            "assigned_sections": assigned_sections,
            "assigned_semesters": assigned_semesters,
        }
    finally:
        if created_here:
            try:
                db.close()
            except Exception:
                pass


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
            branch_section TEXT,
            section TEXT,
            subject_id INTEGER NOT NULL,
            subject_name TEXT,
            period TEXT,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT,
            teacher_id INTEGER,
            marked_at TEXT DEFAULT CURRENT_TIMESTAMP
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
        CREATE TABLE IF NOT EXISTS subject_aliases (
            id SERIAL PRIMARY KEY,
            alias TEXT UNIQUE NOT NULL,
            canonical_name TEXT NOT NULL
        );
        """)

        db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_student_subject_date_period
        ON attendance(student_id, subject_id, date, period);
        """)

        db.execute("""
        CREATE TABLE IF NOT EXISTS attendance_sessions (
            id SERIAL PRIMARY KEY,
            timetable_entry_id INTEGER,
            faculty_name TEXT,
            section TEXT,
            subject_name TEXT,
            date TEXT NOT NULL,
            start_time TEXT,
            end_time TEXT,
            is_closed INTEGER DEFAULT 0,
            UNIQUE(section, subject_name, date, start_time, end_time)
        );
        """)

        db.execute("""
        CREATE TABLE IF NOT EXISTS attendance_records (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            UNIQUE(session_id, student_id)
        );
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
            branch_section TEXT,
            section TEXT,
            subject_id INTEGER NOT NULL,
            subject_name TEXT,
            period TEXT,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT,
            teacher_id INTEGER,
            marked_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            key TEXT UNIQUE NOT NULL,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subject_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT UNIQUE NOT NULL,
            canonical_name TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_student_subject_date_period
        ON attendance(student_id, subject_id, date, period);

        CREATE TABLE IF NOT EXISTS attendance_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timetable_entry_id INTEGER,
            faculty_name TEXT,
            section TEXT,
            subject_name TEXT,
            date TEXT NOT NULL,
            start_time TEXT,
            end_time TEXT,
            is_closed INTEGER DEFAULT 0,
            UNIQUE(section, subject_name, date, start_time, end_time)
        );

        CREATE TABLE IF NOT EXISTS attendance_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES attendance_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
            UNIQUE(session_id, student_id)
        );
        """)

    _ensure_subject_alias_table(db)
    _ensure_timetable_entry_text_columns(db)
    _ensure_attendance_schema(db)

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

    # ✅ One-time migration: rename MECH branch to CSW (merge if both exist)
    try:
        mech_row = db.execute(
            f"SELECT id FROM branches WHERE UPPER(TRIM(name)) = {placeholder}", ("MECH",)
        ).fetchone()
        csw_row = db.execute(
            f"SELECT id FROM branches WHERE UPPER(TRIM(name)) = {placeholder}", ("CSW",)
        ).fetchone()
        if mech_row:
            mech_id = row_get(mech_row, "id")
            if not csw_row:
                # Simple rename when CSW doesn't exist yet
                db.execute(
                    f"UPDATE branches SET name = {placeholder} WHERE id = {placeholder}",
                    ("CSW", mech_id),
                )
                print("[init_db] Renamed branch MECH -> CSW")
            else:
                # CSW already exists; reassign all foreign keys from MECH -> CSW then remove MECH
                csw_id = row_get(csw_row, "id")
                fk_updates = [
                    ("students", "branch_id"),
                    ("subjects", "branch_id"),
                    ("timetable_entries", "branch_id"),
                    ("attendance", "branch_id"),
                    ("teacher_branches", "branch_id"),
                    ("teachers", "branch_id"),
                ]
                for table, col in fk_updates:
                    try:
                        db.execute(f"UPDATE {table} SET {col} = {placeholder} WHERE {col} = {placeholder}", (csw_id, mech_id))
                    except Exception:
                        # Some tables may not exist in older schemas; ignore failures
                        pass
                try:
                    db.execute(f"DELETE FROM branches WHERE id = {placeholder}", (mech_id,))
                except Exception:
                    pass
                print("[init_db] Merged branch MECH into existing CSW (reassigned FK rows)")
    except Exception as _e:
        print(f"[init_db] Branch rename migration skipped: {repr(_e)}")

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
            return redirect(url_for("teacher_login"))
        if session.get("role") == "student":
            flash("You do not have permission to access this page.", "danger")
            return redirect(url_for("student_dashboard"))
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


def get_current_active_classes(db):
    """
    Returns currently running classes based on the current day and time.
    Excludes SHORT BREAK and handles overlapping periods safely.
    """
    now = datetime.now()
    current_day = now.strftime("%A")
    current_time_str = now.strftime("%H:%M")
    
    # Query timetable_entries to find entries where current_time is between start_time and end_time
    # and day matches current_day, excluding SHORT BREAK.
    query = """
        SELECT 
            te.section,
            s.name as subject_name,
            t.name as faculty_name,
            te.room,
            te.start_time,
            te.end_time,
            b.name as branch_name
        FROM timetable_entries te
        LEFT JOIN subjects s ON te.subject_id = s.id
        LEFT JOIN teachers t ON te.teacher_id = t.id
        LEFT JOIN branches b ON te.branch_id = b.id
        WHERE LOWER(TRIM(te.day)) = LOWER(TRIM(%s))
          AND te.start_time <= %s
          AND te.end_time > %s
          AND (s.name IS NULL OR UPPER(TRIM(s.name)) != 'SHORT BREAK')
    """
    
    placeholder = get_placeholder()
    query = query.replace("%s", placeholder)
    
    rows = db.execute(query, (current_day, current_time_str, current_time_str)).fetchall()
    
    active_classes = []
    for row in rows:
        active_classes.append({
            "section": row_get(row, "section", ""),
            "subject": row_get(row, "subject_name", ""),
            "faculty": row_get(row, "faculty_name", ""),
            "room": row_get(row, "room", ""),
            "start_time": row_get(row, "start_time", ""),
            "end_time": row_get(row, "end_time", ""),
            "branch": row_get(row, "branch_name", "")
        })
        
    return active_classes


if "api_active_classes" not in app.view_functions:
    @app.route("/api/active-classes", methods=["GET"])
    def api_active_classes():
        db = None
        try:
            db = get_db()
            classes = get_current_active_classes(db)
            return jsonify({"success": True, "active_classes": classes, "count": len(classes)})
        except Exception as e:
            print(f"Error fetching active classes: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if db:
                try:
                    db.close()
                except Exception:
                    pass


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/dashboard")
@login_required
def dashboard():
    """Main admin dashboard with stats and charts."""
    db = None
    dashboard_context = _dashboard_default_context()
    try:
        db = get_db()
        placeholder = get_placeholder()

        def _safe_scalar(sql, params=(), default=0):
            try:
                row = db.execute(sql, params).fetchone()
                if row is None:
                    return default
                if isinstance(row, tuple):
                    return row[0] if row else default
                # sqlite3.Row / dict-like rows
                try:
                    keys = list(row.keys())
                    return row_get(row, keys[0], default)
                except Exception:
                    return row[0] if row else default
            except Exception as e:
                print(f"[dashboard] query failed scalar: {repr(e)} | sql={sql}")
                return default

        def _safe_rows(sql, params=()):
            try:
                return db.execute(sql, params).fetchall()
            except Exception as e:
                print(f"[dashboard] query failed rows: {repr(e)} | sql={sql}")
                return []

        branch_count = int(_safe_scalar("SELECT COUNT(*) FROM branches", default=0) or 0)
        student_count = int(_safe_scalar("SELECT COUNT(*) FROM students", default=0) or 0)
        subject_count = int(_safe_scalar("SELECT COUNT(*) FROM subjects", default=0) or 0)
        attendance_count = int(_safe_scalar("SELECT COUNT(*) FROM attendance", default=0) or 0)

        attendance_stats = None
        try:
            attendance_stats = db.execute("""
                SELECT
                    COUNT(CASE WHEN status='Present' THEN 1 END) as present_count,
                    COUNT(*) as total_count
                FROM attendance
            """).fetchone()
        except Exception as e:
            print(f"[dashboard] attendance stats query failed: {repr(e)}")

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

        subject_data = _safe_rows(
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
        )

        subject_chart_labels = [row_get(r, "name") for r in subject_data]
        subject_chart_percentages = [float(row_get(r, "percentage") or 0) for r in subject_data]
        
        branch_data = _safe_rows("""
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
        """)

        # Build last-7-days chart data
        chart_dates = [date.today() - timedelta(days=i) for i in range(6, -1, -1)]
        chart_date_values = [d.isoformat() for d in chart_dates]
        chart_data = []
        if chart_date_values:
            chart_rows = _safe_rows(
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
            )
            chart_map = {row_get(r, "date"): r for r in chart_rows}
            for date_str in chart_date_values:
                row = chart_map.get(date_str)
                total_count = row_get(row, "total_count", 0) or 0
                present_count = row_get(row, "present_count", 0) or 0
                percentage = round((present_count / total_count) * 100, 1) if total_count else 0
                chart_data.append({"date": date_str, "percentage": percentage})

        today_percentage = round((present_count / total_classes) * 100, 1) if total_classes > 0 else 0
        today_day = date.today().strftime("%A")
        active_classes_today = int(_safe_scalar(
            f"SELECT COUNT(*) FROM timetable_entries WHERE LOWER(TRIM(day)) = LOWER(TRIM({placeholder}))",
            (today_day,),
            default=0,
        ) or 0)

        total_teachers = int(_safe_scalar("SELECT COUNT(*) FROM teachers", default=0) or 0)
        try:
            total_semesters = int(_safe_scalar(
                "SELECT COUNT(DISTINCT current_semester) FROM students WHERE current_semester IS NOT NULL",
                default=0,
            ) or 0)
        except Exception:
            total_semesters = 0

        try:
            attendance_threshold = int(get_setting(db, "low_attendance_threshold", app.config["LOW_ATTENDANCE_THRESHOLD"]))
        except Exception:
            attendance_threshold = int(app.config["LOW_ATTENDANCE_THRESHOLD"])

        low_alerts_q = f"""
            SELECT COUNT(*) AS c FROM (
                SELECT s.id
                FROM students s
                LEFT JOIN attendance a ON a.student_id = s.id
                GROUP BY s.id
                HAVING COUNT(a.id) > 0
                   AND ROUND(SUM(CASE WHEN a.status = 'Present' THEN 1 ELSE 0 END) * 100.0 / COUNT(a.id), 1) < {placeholder}
            ) sub
        """
        low_attendance_alerts = int(_safe_scalar(low_alerts_q, (attendance_threshold,), default=0) or 0)

        current_active_period = None
        upcoming_timetable = []
        try:
            # Use module imported at startup if available, else attempt import safely
            timetable_module = globals().get('_timetable')
            if timetable_module is None:
                try:
                    import timetable as timetable_module
                    print("[dashboard] timetable module imported at lookup time")
                except Exception as imp_err:
                    timetable_module = None
                    print("[dashboard] timetable import failed:", repr(imp_err))
                    traceback.print_exc()
            if timetable_module:
                try:
                    current_active_period = timetable_module.get_global_active_class(db)
                    upcoming_timetable = timetable_module.get_upcoming_classes(db, "", "", limit=4)
                except Exception as timetable_err:
                    print(f"[dashboard] timetable function call failed: {repr(timetable_err)}")
                    traceback.print_exc()
                    current_active_period = None
                    upcoming_timetable = []
            else:
                current_active_period = None
                upcoming_timetable = []
        except Exception as e:
            print(f"[dashboard] unexpected timetable error: {repr(e)}")
            traceback.print_exc()
            current_active_period = None
            upcoming_timetable = []

        # Recent activity feed (last 10 attendance records)
        recent_activity = _safe_rows("""
            SELECT
                a.date,
                s.name AS student_name,
                a.status,
                sub.name AS subject_name,
                b.name AS branch_name
            FROM attendance a
            LEFT JOIN students s ON a.student_id = s.id
            LEFT JOIN subjects sub ON a.subject_id = sub.id
            LEFT JOIN branches b ON a.branch_id = b.id
            ORDER BY a.id DESC
            LIMIT 10
        """)

        dashboard_context.update({
            "branch_count": branch_count,
            "student_count": student_count,
            "subject_count": subject_count,
            "attendance_count": attendance_count,
            "total_classes": total_classes,
            "present_count": present_count,
            "absent_count": absent_count,
            "overall_percentage": overall_percentage,
            "today_percentage": today_percentage,
            "active_classes_today": active_classes_today,
            "low_attendance_alerts": low_attendance_alerts,
            "total_teachers": total_teachers,
            "total_semesters": total_semesters,
            "subject_data": subject_data,
            "subject_chart_labels": subject_chart_labels,
            "subject_chart_percentages": subject_chart_percentages,
            "branch_data": branch_data,
            "chart_data": chart_data,
            "trend_labels": chart_date_values,
            "trend_percentages": [item["percentage"] for item in chart_data],
            "monthly_labels": chart_date_values,
            "monthly_percentages": [item["percentage"] for item in chart_data],
            "current_active_period": current_active_period,
            "upcoming_timetable": upcoming_timetable,
            "recent_activity": recent_activity,
        })

        db.close()
        return render_template("dashboard.html", **dashboard_context)
    except Exception as e:
        print(f"[dashboard] CRITICAL ERROR: {repr(e)}")
        print(traceback.format_exc())
        flash("Dashboard loaded with limited data due to a database issue.", "warning")
        error_dashboard_context = dict(dashboard_context)
        error_dashboard_context["error_mode"] = True
        return render_template("dashboard.html", **error_dashboard_context)
    finally:
        if db:
            try: db.close()
            except: pass


@app.route("/department-dashboard")
@login_required
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

@app.route("/teachers", methods=["GET", "POST"])
@login_required
def teachers_management():
    if session.get("role") != "admin":
        abort(403)

    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        if request.method == "POST":
            action = (request.form.get("action") or "").strip()
            teacher_id = (request.form.get("teacher_id") or "").strip()

            if action == "add":
                name = (request.form.get("name") or "").strip()
                username = (request.form.get("username") or "").strip()
                password = (request.form.get("password") or "").strip()
                email = (request.form.get("email") or "").strip()
                phone = (request.form.get("phone") or "").strip()
                status = (request.form.get("status") or "active").strip() or "active"

                if not name or not username or not password:
                    flash("Name, username and password are required.", "error")
                else:
                    existing = db.execute(
                        f"SELECT id FROM teachers WHERE username = {placeholder}",
                        (username,),
                    ).fetchone()
                    if existing:
                        flash("A teacher with that username already exists.", "error")
                    else:
                        if str(app.config.get("DATABASE", "")).startswith("postgres"):
                            cur = db.execute(
                                f"INSERT INTO teachers (name, username, password, password_hash, email, phone, status) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}) RETURNING id",
                                (name, username, generate_password_hash(password), generate_password_hash(password), email, phone, status),
                            )
                            new_teacher_id = row_get(cur.fetchone(), "id")
                        else:
                            cur = db.execute(
                                f"INSERT INTO teachers (name, username, password, password_hash, email, phone, status) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
                                (name, username, generate_password_hash(password), generate_password_hash(password), email, phone, status),
                            )
                            new_teacher_id = cur.lastrowid
                            
                        db.execute(
                            f"INSERT INTO users (username, password, role) VALUES ({placeholder}, {placeholder}, {placeholder})",
                            (username, generate_password_hash(password), "teacher"),
                        )
                        
                        # Process dynamic assignment rows
                        assign_subjects = request.form.getlist("assign_subject_id[]")
                        assign_branches = request.form.getlist("assign_branch_id[]")
                        assign_sections = request.form.getlist("assign_section[]")
                        assign_semesters = request.form.getlist("assign_semester[]")
                        for i in range(len(assign_subjects)):
                            s_id = assign_subjects[i]
                            b_id = assign_branches[i] if i < len(assign_branches) else ""
                            sec = assign_sections[i] if i < len(assign_sections) else ""
                            sem = assign_semesters[i] if i < len(assign_semesters) else ""
                            if s_id and b_id:
                                db.execute(
                                    f"INSERT INTO teacher_subject_assignments (teacher_id, subject_id, branch_id, section, semester) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
                                    (int(new_teacher_id), int(s_id), int(b_id), sec, sem)
                                )
                        
                        db.commit()
                        flash("Teacher created.", "success")

            elif action == "edit" and teacher_id.isdigit():
                name = (request.form.get("name") or "").strip()
                username = (request.form.get("username") or "").strip()
                email = (request.form.get("email") or "").strip()
                phone = (request.form.get("phone") or "").strip()
                password = (request.form.get("password") or "").strip()
                status = (request.form.get("status") or "active").strip() or "active"
                if not name or not username:
                    flash("Name and username are required.", "error")
                else:
                    if password:
                        db.execute(
                            f"UPDATE teachers SET name = {placeholder}, username = {placeholder}, email = {placeholder}, phone = {placeholder}, status = {placeholder}, password = {placeholder}, password_hash = {placeholder} WHERE id = {placeholder}",
                            (name, username, email, phone, status, generate_password_hash(password), generate_password_hash(password), int(teacher_id)),
                        )
                        db.execute(
                            f"UPDATE users SET username = {placeholder}, password = {placeholder} WHERE id = {placeholder} AND role = {placeholder}",
                            (username, generate_password_hash(password), int(teacher_id), "teacher"),
                        )
                    else:
                        db.execute(
                            f"UPDATE teachers SET name = {placeholder}, username = {placeholder}, email = {placeholder}, phone = {placeholder}, status = {placeholder} WHERE id = {placeholder}",
                            (name, username, email, phone, status, int(teacher_id)),
                        )
                        db.execute(
                            f"UPDATE users SET username = {placeholder} WHERE id = {placeholder} AND role = {placeholder}",
                            (username, int(teacher_id), "teacher"),
                        )
                        
                    # Process dynamic assignment rows
                    db.execute(f"DELETE FROM teacher_subject_assignments WHERE teacher_id = {placeholder}", (int(teacher_id),))
                    
                    assign_subjects = request.form.getlist("assign_subject_id[]")
                    assign_branches = request.form.getlist("assign_branch_id[]")
                    assign_sections = request.form.getlist("assign_section[]")
                    assign_semesters = request.form.getlist("assign_semester[]")
                    for i in range(len(assign_subjects)):
                        s_id = assign_subjects[i]
                        b_id = assign_branches[i] if i < len(assign_branches) else ""
                        sec = assign_sections[i] if i < len(assign_sections) else ""
                        sem = assign_semesters[i] if i < len(assign_semesters) else ""
                        if s_id and b_id:
                            db.execute(
                                f"INSERT INTO teacher_subject_assignments (teacher_id, subject_id, branch_id, section, semester) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
                                (int(teacher_id), int(s_id), int(b_id), sec, sem)
                            )
                            
                    db.commit()
                    flash("Teacher updated.", "success")

            elif action == "delete" and teacher_id.isdigit():
                db.execute(f"DELETE FROM teachers WHERE id = {placeholder}", (int(teacher_id),))
                db.execute(f"DELETE FROM users WHERE username = {placeholder} AND role = {placeholder}", (username, "teacher"))
                db.commit()
                flash("Teacher deleted.", "success")

            elif action == "reset_password" and teacher_id.isdigit():
                new_password = (request.form.get("new_password") or "").strip()
                if len(new_password) < 4:
                    flash("Password must be at least 4 characters.", "error")
                else:
                    db.execute(
                        f"UPDATE teachers SET password = {placeholder}, password_hash = {placeholder} WHERE id = {placeholder}",
                        (generate_password_hash(new_password), generate_password_hash(new_password), int(teacher_id)),
                    )
                    db.execute(
                        f"UPDATE users SET password = {placeholder} WHERE id = {placeholder} AND role = {placeholder}",
                        (generate_password_hash(new_password), int(teacher_id), "teacher"),
                    )
                    db.commit()
                    flash("Password reset successfully.", "success")

        teachers = db.execute(
            f"SELECT id, name, username, password, password_hash, email, phone, status FROM teachers ORDER BY name"
        ).fetchall()
        subjects = db.execute("SELECT id, name FROM subjects ORDER BY name").fetchall()
        branches = db.execute("SELECT id, name FROM branches ORDER BY name").fetchall()
        teacher_assignments = {}
        teacher_subjects_map = {}
        teacher_branches_map = {}
        rows = db.execute("SELECT tsa.teacher_id, tsa.subject_id, tsa.branch_id, tsa.section, tsa.semester, tsa.academic_year, s.name as subject_name, b.name as branch_name FROM teacher_subject_assignments tsa LEFT JOIN subjects s ON s.id = tsa.subject_id LEFT JOIN branches b ON b.id = tsa.branch_id ORDER BY tsa.teacher_id").fetchall()
        for row in rows:
            tid = row_get(row, "teacher_id")
            s_id = row_get(row, "subject_id")
            b_id = row_get(row, "branch_id")
            s_name = row_get(row, "subject_name")
            b_name = row_get(row, "branch_name")
            
            teacher_assignments.setdefault(tid, []).append({
                "subject_id": s_id,
                "branch_id": b_id,
                "section": row_get(row, "section") or "",
                "semester": row_get(row, "semester") or "",
                "academic_year": row_get(row, "academic_year") or "",
            })
            if s_id and s_id not in [x["id"] for x in teacher_subjects_map.setdefault(tid, [])]:
                teacher_subjects_map[tid].append({"id": s_id, "name": s_name})
            if b_id and b_id not in [x["id"] for x in teacher_branches_map.setdefault(tid, [])]:
                teacher_branches_map[tid].append({"id": b_id, "name": b_name})

        return render_template(
            "admin_teachers.html",
            teachers=teachers,
            subjects=subjects,
            branches=branches,
            teacher_assignments=teacher_assignments,
            teacher_subjects_map=teacher_subjects_map,
            teacher_branches_map=teacher_branches_map,
        )
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


@app.route("/assign-teachers", methods=["GET", "POST"])
@login_required
def assign_teachers():
    if session.get("role") != "admin":
        abort(403)

    db = None
    try:
        db = get_db()
        if request.method == "POST":
            teacher_id = (request.form.get("teacher_id") or "").strip()
            subject_id = (request.form.get("subject_id") or "").strip()
            branch_id = (request.form.get("branch_id") or "").strip()
            section = (request.form.get("section") or "").strip()
            semester = (request.form.get("semester") or "").strip()
            academic_year = (request.form.get("academic_year") or "").strip()
            if not teacher_id or not subject_id or not branch_id:
                flash("Teacher, subject and branch are required.", "error")
            else:
                db.execute(
                    f"DELETE FROM teacher_subject_assignments WHERE teacher_id = {placeholder} AND subject_id = {placeholder} AND branch_id = {placeholder}",
                    (int(teacher_id), int(subject_id), int(branch_id)),
                )
                db.execute(
                    f"INSERT INTO teacher_subject_assignments (teacher_id, subject_id, branch_id, section, semester, academic_year) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
                    (int(teacher_id), int(subject_id), int(branch_id), section, semester, academic_year),
                )
                db.commit()
                flash("Assignment saved.", "success")

        teachers = db.execute("SELECT id, name, username FROM teachers ORDER BY name").fetchall()
        subjects = db.execute("SELECT id, name FROM subjects ORDER BY name").fetchall()
        branches = db.execute("SELECT id, name FROM branches ORDER BY name").fetchall()
        assignments = db.execute(
            "SELECT tsa.id, tsa.teacher_id, tsa.subject_id, tsa.branch_id, tsa.section, tsa.semester, tsa.academic_year, t.name AS teacher_name, s.name AS subject_name, b.name AS branch_name FROM teacher_subject_assignments tsa JOIN teachers t ON t.id = tsa.teacher_id JOIN subjects s ON s.id = tsa.subject_id JOIN branches b ON b.id = tsa.branch_id ORDER BY t.name, s.name, b.name"
        ).fetchall()

        return render_template(
            "assign_teachers.html",
            teachers=teachers,
            subjects=subjects,
            branches=branches,
            assignments=assignments,
        )
    finally:
        if db is not None:
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
        action   = request.form.get("action", "add").strip()
        name     = request.form.get("name", "").strip()
        location = request.form.get("location", "").strip()
        branch_id_raw = request.form.get("branch_id", "").strip()

        if action == "edit":
            # ── Rename an existing branch ──────────────────────────────────────
            branch_id_val = _coerce_int(branch_id_raw)
            if not branch_id_val:
                flash("Invalid branch selected for editing.", "error")
            elif not name:
                flash("Branch name is required.", "error")
            else:
                try:
                    db.execute(
                        f"UPDATE branches SET name = {placeholder}, location = {placeholder} "
                        f"WHERE id = {placeholder}",
                        (name, location, branch_id_val),
                    )
                    db.commit()
                    flash(f"Branch renamed to '{name}' successfully.", "success")
                except Exception as e:
                    db.rollback()
                    print(f"Error updating branch: {e}")
                    flash("Could not rename branch. Name may already exist.", "error")

        elif action == "delete":
            # ── Delete a branch ────────────────────────────────────────────────
            branch_id_val = _coerce_int(branch_id_raw)
            if not branch_id_val:
                flash("Invalid branch selected for deletion.", "error")
            else:
                try:
                    db.execute(
                        f"DELETE FROM branches WHERE id = {placeholder}",
                        (branch_id_val,),
                    )
                    db.commit()
                    flash("Branch deleted successfully.", "success")
                except Exception as e:
                    db.rollback()
                    print(f"Error deleting branch: {e}")
                    flash("Could not delete branch. It may be in use.", "error")

        else:
            # ── Add one or more branches (action == "add") ─────────────────────
            if not name:
                flash("Branch name is required.", "error")
            else:
                sections_raw = request.form.get("sections", "").strip()
                # Build list of names to insert
                if sections_raw:
                    section_list = [s.strip() for s in sections_raw.split(",") if s.strip()]
                    names_to_insert = [f"{name}-{s}" for s in section_list]
                else:
                    names_to_insert = [name]

                added = 0
                already_exists = []
                is_postgres = str(app.config.get("DATABASE", "")).startswith("postgres")
                for branch_name in names_to_insert:
                    try:
                        if is_postgres:
                            result = db.execute(
                                f"INSERT INTO branches (name, location) VALUES ({placeholder}, {placeholder}) "
                                f"ON CONFLICT (name) DO NOTHING RETURNING id",
                                (branch_name, location),
                            ).fetchone()
                            if result:
                                added += 1
                            else:
                                already_exists.append(branch_name)
                        else:
                            cur = db.execute(
                                f"INSERT OR IGNORE INTO branches (name, location) VALUES ({placeholder}, {placeholder})",
                                (branch_name, location),
                            )
                            if getattr(cur, "rowcount", 0) > 0:
                                added += 1
                            else:
                                already_exists.append(branch_name)
                    except Exception as e:
                        db.rollback()
                        print(f"Error adding branch '{branch_name}': {e}")
                        flash(f"Error adding branch '{branch_name}'.", "error")

                if added:
                    db.commit()
                    flash(f"{added} branch(es) added successfully.", "success")
                if already_exists:
                    flash(f"Already exists (skipped): {', '.join(already_exists)}", "warning")

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

        # Derive branch name from the uploaded filename
        # Example: "ECE-B.xlsx" -> "ECE-B", "CSE-A (1).xlsx" -> "CSE-A (1)"
        # This is the ONLY safe source of truth for the branch -- never use
        # generated placeholder names like COPILOT_BRANCH_*.
        import os as _os
        filename_branch_name = _os.path.splitext(filename)[0].strip()
        # Remove any parenthesised copy-number suffix added by the OS, e.g. " (1)"
        import re as _re
        filename_branch_name = _re.sub(r'\s*\(\d+\)$', '', filename_branch_name).strip()


        try:
            import pandas as pd
        except Exception:
            flash("pandas is not installed. Please add pandas and openpyxl to requirements.", "error")
            return redirect(url_for("upload_students"))

        try:
            # Read the file ONCE into memory to save RAM
            df_full = pd.read_excel(file, header=None)

            if df_full.empty:
                flash("The Excel file is empty.", "error")
                return redirect(url_for("upload_students"))

            # Find the header row by searching for keywords in the first 50 rows
            header_idx = 0
            for i, row in df_full.head(50).iterrows():
                row_str = " ".join([str(cell).lower() for cell in row])
                if any(k in row_str for k in ["name", "enrollment", "h.t.no", "mail", "branch", "section"]):
                    header_idx = i
                    break

            # Slice the existing dataframe instead of re-reading from disk
            df = df_full.iloc[header_idx + 1:].copy()
            df.columns = [str(c).strip() for c in df_full.iloc[header_idx].tolist()]
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
            'section': 'branch_id',
        }


        # Normalize existing columns and rename based on mapping
        current_cols = [str(c).strip().lower() for c in df.columns]
        new_cols = []
        for col in current_cols:
            found = False
            for alias, target in column_mapping.items():
                if alias in col:
                    new_cols.append(target)
                    found = True
                    break
            if not found:
                new_cols.append(col)
        df.columns = new_cols

        # Minimum required columns (branch_id is optional when filename provides it)
        core_required = {"name", "enrollment"}
        missing = core_required - set(df.columns)
        if missing:
            flash(
                f"Missing columns: {', '.join(sorted(missing))}. "
                "We searched for keywords like 'Name', 'Enrollment', 'Mail', 'Branch', and 'Section'.",
                "error",
            )

            return redirect(url_for("upload_students"))

        db = get_db()
        placeholder = get_placeholder()
        is_postgres = str(app.config.get("DATABASE", "")).startswith("postgres")
        inserted = 0
        skipped = 0
        errors = 0

        try:
            # Resolve / auto-create the branch from the filename.
            # Filename is the authoritative branch source; data-column branch_id
            # is used only as a fallback when the file has multiple branches.
            filename_branch_id = None
            if filename_branch_name:
                br_row = db.execute(
                    f"SELECT id FROM branches WHERE UPPER(name) = {placeholder}",
                    (filename_branch_name.upper(),),
                ).fetchone()
                if br_row:
                    filename_branch_id = row_get(br_row, "id")
                else:
                    # Auto-create the branch so every import works regardless
                    # of whether the admin pre-created it.
                    print(f"[upload_students] Auto-creating branch '{filename_branch_name}' from filename.")
                    if is_postgres:
                        new_br = db.execute(
                            f"INSERT INTO branches (name, location) VALUES ({placeholder}, {placeholder}) "
                            f"ON CONFLICT (name) DO NOTHING RETURNING id",
                            (filename_branch_name, ""),
                        ).fetchone()
                        if new_br:
                            filename_branch_id = row_get(new_br, "id")
                        else:
                            # Concurrent insert by another worker -- just look it up
                            br_row2 = db.execute(
                                f"SELECT id FROM branches WHERE UPPER(name) = {placeholder}",
                                (filename_branch_name.upper(),),
                            ).fetchone()
                            filename_branch_id = row_get(br_row2, "id") if br_row2 else None
                    else:
                        cur = db.execute(
                            f"INSERT OR IGNORE INTO branches (name, location) VALUES ({placeholder}, {placeholder})",
                            (filename_branch_name, ""),
                        )
                        if getattr(cur, "rowcount", 0) > 0:
                            filename_branch_id = cur.lastrowid
                        else:
                            br_row2 = db.execute(
                                f"SELECT id FROM branches WHERE UPPER(name) = {placeholder}",
                                (filename_branch_name.upper(),),
                            ).fetchone()
                            filename_branch_id = row_get(br_row2, "id") if br_row2 else None
                    if filename_branch_id:
                        db.commit()  # Commit the new branch before inserting students

            # Pre-fetch all branches for data-column fallback matching
            branches_map = {}
            for b in db.execute("SELECT id, name FROM branches").fetchall():
                b_name = row_get(b, "name")
                b_id = row_get(b, "id")
                if b_name is not None:
                    branches_map[str(b_name).lower()] = b_id
                if b_id is not None:
                    branches_map[str(b_id)] = b_id
                    
            csw_id = branches_map.get("csw")


            for _, row in df.iterrows():
                name       = str(row.get("name", "")).strip()
                enrollment = str(row.get("enrollment", "")).strip()
                email      = str(row.get("email", "") or "").strip()


                if not name or not enrollment:
                    errors += 1
                    continue

                # Prefer filename-derived branch; fall back to data column
                branch_id = filename_branch_id

                if branch_id is None and "branch_id" in df.columns:
                    branch_id_raw = str(row.get("branch_id", "")).strip()
                    if branch_id_raw and branch_id_raw.lower() not in ("nan", "none", ""):
                        branch_id_raw_lower = branch_id_raw.lower()
                        if branch_id_raw_lower in branches_map:
                            branch_id = branches_map[branch_id_raw_lower]
                        else:
                            for b_name, b_id in branches_map.items():
                                if b_name and b_name in branch_id_raw_lower:
                                    branch_id = b_id
                                    break

                if branch_id is None:
                    print(f"[upload_students] Could not resolve branch for row (enrollment={enrollment})")
                    errors += 1
                    continue

                # CSW branch validation
                if csw_id and branch_id == csw_id:
                    enr_upper = enrollment.upper()
                    if not (enr_upper.startswith("25TQ1A56") and enr_upper[8:].isdigit() and 1 <= int(enr_upper[8:]) <= 61):
                        print(f"[upload_students] Skipped: {enrollment} is invalid for CSW branch.")
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
                        f"INSERT OR IGNORE INTO students (name, enrollment, email, branch_id) "
                        f"VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
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
                        f"INSERT OR IGNORE INTO users (username, password, role, student_id) "
                        f"VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
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
def upload_students_csv():
    """CSV upload for students — streams row-by-row, safe int coercion, PostgreSQL + SQLite."""

    def _safe_int_csv(value):
        """Convert CSV cell to int, returning None for blank/nan/non-numeric values.

        Handles the common cases that break PostgreSQL integer columns:
          "1.0"  -> 1
          "2.0"  -> 2
          ""     -> None
          "nan"  -> None
          "N/A"  -> None
        """
        if value is None:
            return None
        s = str(value).strip()
        if not s or s.lower() in ("nan", "none", "n/a", "null", "-"):
            return None
        try:
            return int(float(s))
        except (ValueError, OverflowError):
            return None

    if request.method != "POST":
        return render_template("upload_students_csv.html")

    # ── 1. Validate uploaded file ──────────────────────────────────────────────
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Please upload a CSV file.", "error")
        return redirect(url_for("upload_students_csv"))

    fname = secure_filename(file.filename)
    if not fname.lower().endswith(".csv"):
        flash("Only .csv files are supported.", "error")
        return redirect(url_for("upload_students_csv"))

    # ✅ Extract branch name from filename (e.g., "ECE-B.csv" → "ECE-B")
    branch_name_from_file = fname.rsplit(".", 1)[0].strip().upper()
    if not branch_name_from_file:
        flash("Invalid filename — cannot determine branch name.", "error")
        return redirect(url_for("upload_students_csv"))

    # ── 2. Parse header row via stdlib csv (no pandas → no large memory spike) ─
    import csv
    import io

    try:
        # Wrap the file stream in a TextIOWrapper to stream it line-by-line
        # instead of reading the entire file into memory at once.
        # This keeps memory usage extremely low.
        text_stream = io.TextIOWrapper(file.stream, encoding="utf-8-sig", errors="replace")
        reader = csv.DictReader(text_stream)
    except Exception as e:
        print(f"[upload_students_csv] Failed to read CSV: {repr(e)}")
        flash("Could not read the CSV file. Make sure it is UTF-8 encoded.", "error")
        return redirect(url_for("upload_students_csv"))

    # Normalise header names to lowercase+stripped so column casing doesn't matter
    if reader.fieldnames is None:
        flash("The CSV file appears to be empty.", "error")
        return redirect(url_for("upload_students_csv"))

    fieldnames_norm = [str(f).strip().lower() for f in reader.fieldnames]
    required = {"name", "enrollment", "email"}
    missing = required - set(fieldnames_norm)
    if missing:
        flash(
            f"CSV is missing required columns: {', '.join(sorted(missing))}. "
            "Expected headers: name, enrollment, email",
            "error",
        )
        return redirect(url_for("upload_students_csv"))

    # ── 3. Create or reuse branch from filename ────────────────────────────────
    db = get_db()
    placeholder = get_placeholder()
    is_postgres = str(app.config.get("DATABASE", "")).startswith("postgres")

    existing_branch = db.execute(
        f"SELECT id FROM branches WHERE UPPER(TRIM(name)) = {placeholder}",
        (branch_name_from_file,),
    ).fetchone()
    if existing_branch:
        branch_id_from_filename = row_get(existing_branch, "id")
    else:
        try:
            db.execute(
                f"INSERT INTO branches (name) VALUES ({placeholder})",
                (branch_name_from_file,),
            )
            new_branch = db.execute(
                f"SELECT id FROM branches WHERE UPPER(TRIM(name)) = {placeholder}",
                (branch_name_from_file,),
            ).fetchone()
            branch_id_from_filename = row_get(new_branch, "id")
        except Exception as e:
            print(f"[upload_students_csv] Failed to create branch '{branch_name_from_file}': {repr(e)}")
            flash(f"Failed to create branch from filename. Error: {repr(e)}", "error")
            return redirect(url_for("upload_students_csv"))

    # ── 4. Stream rows into the database ──────────────────────────────────────

    total = 0
    inserted = 0
    skipped = 0
    failed = 0
    BATCH_SIZE = 50  # commit every N rows to cap transaction size

    try:
        for raw_row in reader:
            total += 1

            # Normalise keys to lowercase so the dict lookup is case-insensitive
            row = {str(k).strip().lower(): v for k, v in raw_row.items()}

            # ── Extract & clean fields ─────────────────────────────────────
            name       = str(row.get("name", "") or "").strip()
            enrollment = str(row.get("enrollment", "") or "").strip()
            email_raw  = str(row.get("email", "") or "").strip()

            # Skip rows missing the two mandatory fields
            if not name or not enrollment:
                print(f"[upload_students_csv] Row {total}: skipping — missing name or enrollment")
                failed += 1
                continue

            # ✅ Use branch from filename, not from file content
            branch_id = branch_id_from_filename
            
            # CSW branch validation
            if branch_name_from_file == "CSW":
                enr_upper = enrollment.upper()
                if not (enr_upper.startswith("25TQ1A56") and enr_upper[8:].isdigit() and 1 <= int(enr_upper[8:]) <= 61):
                    print(f"[upload_students_csv] Row {total}: skipping — invalid enrollment for CSW branch ({enrollment})")
                    failed += 1
                    continue

            email_value = email_raw if email_raw and email_raw.lower() not in ("nan", "none", "n/a") else None

            # ── Per-row DB work wrapped in its own try/except ──────────────
            try:
                # Duplicate check
                existing = db.execute(
                    f"SELECT id FROM students WHERE enrollment = {placeholder}",
                    (enrollment,),
                ).fetchone()
                if existing:
                    skipped += 1
                    continue

                # Insert student
                if is_postgres:
                    cur = db.execute(
                        f"""
                        INSERT INTO students (name, enrollment, email, branch_id)
                        VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                        ON CONFLICT (enrollment) DO NOTHING
                        RETURNING id
                        """,
                        (name, enrollment, email_value, branch_id),
                    )
                    student_row = cur.fetchone()
                    student_id = row_get(student_row, "id") if student_row else None
                    if not student_id:
                        # Conflict — already exists
                        skipped += 1
                        continue
                else:
                    cur = db.execute(
                        f"INSERT OR IGNORE INTO students (name, enrollment, email, branch_id) "
                        f"VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
                        (name, enrollment, email_value, branch_id),
                    )
                    if getattr(cur, "rowcount", 0) == 0:
                        skipped += 1
                        continue
                    student_id = cur.lastrowid

                # Create student user account (password = last 4 digits of enrollment)
                # Use _fast_student_hash: ~2 ms vs ~400 ms default — prevents Gunicorn timeout
                password_plain = enrollment[-4:] if len(enrollment) >= 4 else enrollment
                password_hash  = _fast_student_hash(password_plain)

                if is_postgres:
                    db.execute(
                        f"""
                        INSERT INTO users (username, password, role, student_id)
                        VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                        ON CONFLICT (username) DO NOTHING
                        """,
                        (enrollment, password_hash, "student", student_id),
                    )
                else:
                    db.execute(
                        f"INSERT OR IGNORE INTO users (username, password, role, student_id) "
                        f"VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
                        (enrollment, password_hash, "student", student_id),
                    )

                inserted += 1

                # Commit in small batches to keep transaction size manageable
                if inserted % BATCH_SIZE == 0:
                    db.commit()

            except Exception as row_err:
                # Roll back only the current transaction so subsequent rows can proceed
                print(
                    f"[upload_students_csv] Row {total} ({enrollment}): "
                    f"DB error — {repr(row_err)}"
                )
                try:
                    db.rollback()
                except Exception:
                    pass
                failed += 1
                continue

        # Final commit for the last partial batch
        try:
            db.commit()
        except Exception as commit_err:
            print(f"[upload_students_csv] Final commit error: {repr(commit_err)}")

    except Exception as fatal_err:
        print(f"[upload_students_csv] FATAL: {repr(fatal_err)}")
        try:
            db.rollback()
        except Exception:
            pass
        flash(
            f"CSV import aborted due to an unexpected error. "
            f"Processed {total} rows before failure. "
            f"Inserted: {inserted}, Skipped: {skipped}, Failed: {failed}.",
            "error",
        )
        return redirect(url_for("upload_students_csv"))
    finally:
        try:
            db.close()
        except Exception:
            pass

    flash(
        f"Import complete — Total: {total} | Inserted: {inserted} | "
        f"Skipped (duplicates): {skipped} | Failed: {failed}.",
        "success" if failed == 0 else "warning",
    )
    return redirect(url_for("students"))


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
                            student_id = _safe_fetchone_value(cur.fetchone(), default=0)
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
        query += " ORDER BY students.enrollment"
        
        students_list = db.execute(query, params).fetchall()
        branches_list = db.execute("SELECT id, name FROM branches ORDER BY name").fetchall()
        db.close()
        return render_template("students.html", students=students_list, branches=branches_list)
    except Exception as e:
        print(f"[students] ERROR: {repr(e)}")
        if db:
            try: db.close()
            except: pass
        flash("Student management is temporarily unavailable.", "error")
        return redirect(url_for("dashboard"))


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

            # ── Primary lookup: exact username match ───────────────────────
            user = db.execute(
                f"SELECT id, username, password, role, student_id FROM users WHERE username = {placeholder}",
                (username,),
            ).fetchone()

            # ── Fallback: some old CSV imports stored enrollment as "12345.0"
            #    (pandas float → str). Try matching via students.enrollment so
            #    students who type their clean enrollment can still log in.
            if not user:
                student_row = db.execute(
                    f"SELECT id FROM students WHERE enrollment = {placeholder}",
                    (username,),
                ).fetchone()
                if student_row:
                    student_id_fb = row_get(student_row, "id")
                    user = db.execute(
                        f"""
                        SELECT u.id, u.username, u.password, u.role, u.student_id
                        FROM users u
                        WHERE u.student_id = {placeholder} AND u.role = 'student'
                        LIMIT 1
                        """,
                        (student_id_fb,),
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


@app.route("/repair_student_logins", methods=["GET", "POST"])
@login_required
def repair_student_logins():
    """Admin-only: backfill missing users rows for students and fix malformed usernames.

    Covers two problems left by the old CSV import:
    1. Students inserted but user account INSERT never ran (row errored out).
    2. users.username stored as '12345.0' instead of '12345' (pandas float artifact).
    """
    if session.get("role") != "admin":
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))

    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        is_postgres = str(app.config.get("DATABASE", "")).startswith("postgres")

        # ── Step 1: fix malformed usernames stored as float strings ("12345.0") ─
        fixed_usernames = 0
        try:
            malformed = db.execute(
                "SELECT u.id, u.username, s.enrollment "
                "FROM users u JOIN students s ON u.student_id = s.id "
                "WHERE u.role = 'student' AND u.username != s.enrollment"
            ).fetchall()
            for row in malformed:
                u_id = row_get(row, "id")
                correct_enrollment = str(row_get(row, "enrollment") or "").strip()
                if not correct_enrollment:
                    continue
                try:
                    db.execute(
                        f"UPDATE users SET username = {placeholder} WHERE id = {placeholder}",
                        (correct_enrollment, u_id),
                    )
                    fixed_usernames += 1
                    print(f"[repair_student_logins] Fixed username id={u_id} -> '{correct_enrollment}'")
                except Exception as upd_err:
                    print(f"[repair_student_logins] Could not fix user id={u_id}: {repr(upd_err)}")
                    try:
                        db.rollback()
                    except Exception:
                        pass
        except Exception as e:
            print(f"[repair_student_logins] Username fix scan failed: {repr(e)}")

        # ── Step 2: create missing user accounts for students with no users row ─
        created = 0
        skipped = 0
        students_without_login = db.execute(
            "SELECT s.id, s.enrollment FROM students s "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM users u WHERE u.student_id = s.id AND u.role = 'student'"
            ")"
        ).fetchall()

        for s_row in students_without_login:
            s_id = row_get(s_row, "id")
            enrollment = str(row_get(s_row, "enrollment") or "").strip()
            if not enrollment or enrollment.lower() in ("nan", "none", ""):
                skipped += 1
                print(f"[repair_student_logins] Skipping student id={s_id} — blank enrollment")
                continue

            password_plain = enrollment[-4:] if len(enrollment) >= 4 else enrollment
            password_hash = _fast_student_hash(password_plain)

            try:
                if is_postgres:
                    db.execute(
                        f"""
                        INSERT INTO users (username, password, role, student_id)
                        VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                        ON CONFLICT (username) DO NOTHING
                        """,
                        (enrollment, password_hash, "student", s_id),
                    )
                else:
                    db.execute(
                        f"INSERT OR IGNORE INTO users (username, password, role, student_id) "
                        f"VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
                        (enrollment, password_hash, "student", s_id),
                    )
                created += 1
                print(f"[repair_student_logins] Created login for student id={s_id} enrollment='{enrollment}'")
            except Exception as ins_err:
                print(f"[repair_student_logins] Insert failed for student id={s_id}: {repr(ins_err)}")
                try:
                    db.rollback()
                except Exception:
                    pass
                skipped += 1

        try:
            db.commit()
        except Exception as ce:
            print(f"[repair_student_logins] Commit error: {repr(ce)}")

        msg = (
            f"Repair complete: {created} logins created, "
            f"{fixed_usernames} usernames corrected, "
            f"{skipped} skipped."
        )
        flash(msg, "success")
        print(f"[repair_student_logins] {msg}")

    except Exception as e:
        print(f"[repair_student_logins] FATAL: {repr(e)}")
        print(traceback.format_exc())
        if db:
            try:
                db.rollback()
            except Exception:
                pass
        flash("Repair failed — check server logs.", "error")
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass

    return redirect(url_for("students"))


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
                # Initialize teacher session fields for downstream routes/templates
                session.clear()
                teacher_id = row_get(user, "id")
                session["user_id"] = teacher_id
                session["username"] = row_get(user, "username")
                session["role"] = row_get(user, "role")
                # Try to populate teacher-specific session values
                try:
                    assigned = db.execute(f"SELECT id, name FROM teachers WHERE id = {placeholder}", (teacher_id,)).fetchone()
                    session["teacher_id"] = teacher_id
                    session["teacher_name"] = row_get(assigned, "name") if assigned else session.get("username")
                except Exception:
                    session["teacher_id"] = teacher_id
                    session["teacher_name"] = session.get("username")
                session.permanent = True
                return redirect(url_for("teacher_dashboard"))

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
                    SELECT id, section FROM teacher_subject_assignments
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
        teacher_id = session.get("teacher_id") or row_get(teacher, "id")

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
            app.logger.info(
                "teacher_dashboard active-slot lookup branch=%s section=%s subject_id=%s teacher_id=%s",
                current_branch_name or "",
                current_section or "",
                subject_id,
                teacher_id,
            )
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
            app.logger.info(
                "teacher_dashboard active-slot result branch=%s section=%s matched=%s subject=%s teacher=%s",
                current_branch_name or "",
                current_section or "",
                bool(active_slot),
                (active_slot.get("subject_name") if isinstance(active_slot, dict) else active_slot["subject_name"] if active_slot and hasattr(active_slot, "keys") and "subject_name" in active_slot.keys() else None),
                (active_slot.get("teacher_name") if isinstance(active_slot, dict) else active_slot["teacher_name"] if active_slot and hasattr(active_slot, "keys") and "teacher_name" in active_slot.keys() else None),
            )
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
        if branch_request_id:
            if branch_request_id in allowed_branch_ids:
                session["teacher_branch_id"] = branch_request_id
            else:
                flash("Invalid branch selection.", "error")
                return "Unauthorized Access", 403

        if subject_request_id:
            session["teacher_subject_id"] = subject_request_id

        if branch_request_id or subject_request_id:
            teacher = get_teacher_context(db)

        current_branch_id = teacher["current_branch_id"]
        current_branch_name = teacher["current_branch_name"]
        current_section = (
            request.args.get("section")
            or session.get("teacher_section")
            or teacher.get("current_section")
            or _branch_section_from_name(current_branch_name or "")
            or ""
        ).strip()
        if current_section:
            session["teacher_section"] = current_section

        if not current_branch_id:
            flash("No branch selected.", "error")
            return redirect(url_for("teacher_select_branch"))

        today_str = date.today().isoformat()
        selected_date = request.args.get("date") or today_str
        try:
            selected_date_obj = date.fromisoformat(selected_date)
        except Exception:
            selected_date_obj = date.today()
        if selected_date_obj > date.today():
            selected_date_obj = date.today()
        selected_date = selected_date_obj.isoformat()
        weekday = selected_date_obj.strftime("%A")
        weekday_short = selected_date_obj.strftime("%a")
        period = (request.args.get("period") or "").strip()

        branch_sections = []
        branch_subjects = []
        today_context = {
            "schedule_rows": [],
            "slots": [],
            "selected_slot": None,
            "active_slot": None,
            "has_schedule": False,
            "schedule_message": "Select a section to load today's timetable.",
        }
        timetable_context = {
            "selected_slot": None,
            "active_slot": None,
            "selected_period": None,
            "active_period": None,
            "can_mark_attendance": False,
            "schedule_message": "Select a section and subject to load students.",
            "current_class_message": "Select a section and subject to load students.",
            "next_class_message": "",
            "has_schedule": False,
        }

        if current_branch_id:
            try:
                branch_sections = _get_timetable_sections_for_branch(db, current_branch_id)
            except Exception as section_error:
                print(f"[teacher_mark_attendance] Section lookup failed: {repr(section_error)}")
                branch_sections = []
            if not current_section and branch_sections:
                current_section = branch_sections[0]
                session["teacher_section"] = current_section

        no_schedule_reason = ""
        if current_branch_id and current_section:
            try:
                branch_subjects = _get_timetable_subjects_for_branch(
                    db,
                    current_branch_id,
                    section=current_section,
                    weekday=weekday,
                    weekday_short=weekday_short,
                )
            except Exception as subject_error:
                print(f"[teacher_mark_attendance] Subject lookup failed: {repr(subject_error)}")
                branch_subjects = []
            try:
                today_context = _resolve_timetable_slots(
                    db,
                    current_branch_id,
                    "",
                    selected_date,
                    section=current_section,
                )
            except Exception as slot_error:
                print(f"[teacher_mark_attendance] Timetable resolve failed: {repr(slot_error)}")
                today_context = today_context
            if not today_context.get("has_schedule"):
                no_schedule_reason = _attendance_no_schedule_reason(
                    db,
                    current_branch_id,
                    section=current_section,
                    weekday=weekday,
                )

        current_subject_token = (
            request.args.get("subject_id")
            or session.get("teacher_subject_id")
            or ""
        ).strip()
        if not current_subject_token and today_context.get("selected_slot"):
            current_subject_token = str(
                today_context.get("selected_slot", {}).get("subject_id")
                or today_context.get("selected_slot", {}).get("subject_name")
                or ""
            ).strip()
        if not current_subject_token and len(branch_subjects) == 1:
            current_subject_token = str(branch_subjects[0].get("id") or "").strip()

        selected_subject = None
        if current_subject_token:
            token_norm = current_subject_token.lower()
            for subject in branch_subjects:
                subject_value = str(subject.get("id") or "").strip()
                subject_name_value = str(subject.get("name") or "").strip()
                if token_norm == subject_value.lower() or token_norm == subject_name_value.lower():
                    selected_subject = subject
                    break

        if selected_subject is not None:
            session["teacher_subject_id"] = selected_subject.get("id")

        selected_subject_value = selected_subject.get("id") if selected_subject else current_subject_token
        selected_subject_name = selected_subject.get("name") if selected_subject else ""

        if current_branch_id and current_section:
            if selected_subject_value:
                timetable_context = _resolve_timetable_slots(
                    db,
                    current_branch_id,
                    selected_subject_value,
                    selected_date,
                    section=current_section,
                    period=period,
                )
            else:
                timetable_context = today_context

        selected_period = timetable_context.get("selected_slot")
        current_active_period = today_context.get("active_slot") or timetable_context.get("active_slot")
        if selected_period and selected_period.get("period"):
            period = str(selected_period.get("period"))

        schedule_message = today_context.get("schedule_message") or "Select a section and subject to load students."
        if no_schedule_reason:
            schedule_message = no_schedule_reason
        can_mark_attendance = bool(timetable_context.get("can_mark_attendance") and selected_period)

        subject_id = selected_period.get("subject_id") if selected_period else None
        subject_name = selected_period.get("subject_name") if selected_period else selected_subject_name
        if subject_name and not selected_subject_name:
            selected_subject_name = subject_name

        subject_names = [str(s.get("name") or "").strip() for s in branch_subjects]
        print(
            f"[attendance] selected branch={current_branch_id} section={current_section!r} weekday={weekday} timetable_rows={len(today_context.get('schedule_rows', []))} subjects={subject_names} active_period={(current_active_period or {}).get('period') if current_active_period else ''}"
        )

        students = []
        attendance_map = {}
        if current_branch_id and current_section and selected_subject_value and period and selected_period:
            students = db.execute(
                f"SELECT id, name, enrollment, roll_no, section FROM students WHERE branch_id = {placeholder} AND (COALESCE(section, '') = COALESCE({placeholder}, '') OR COALESCE(section, '') = '') ORDER BY COALESCE(import_order, id), id",
                (current_branch_id, current_section),
            ).fetchall()

            subject_clause, subject_params = _attendance_subject_clause(placeholder, subject_id, subject_name)
            if subject_clause != "1=0":
                for row in db.execute(
                    f"""
                    SELECT student_id, status, note
                    FROM attendance
                    WHERE branch_id = {placeholder}
                      AND {subject_clause}
                      AND date = {placeholder}
                      AND period = {placeholder}
                    """,
                    tuple([current_branch_id] + subject_params + [selected_date, period]),
                ).fetchall():
                    attendance_map[str(row_get(row, "student_id"))] = row

        if request.method == "POST":
            selected_date = request.form.get("date") or today_str
            period = (request.form.get("period") or "").strip()
            form_branch_id = request.form.get("branch_id") or current_branch_id
            form_section = (request.form.get("section") or current_section).strip()
            form_subject_id = (request.form.get("subject_id") or str(selected_subject_value or "")).strip()
            if form_branch_id:
                current_branch_id = form_branch_id
            if form_section:
                current_section = form_section
                session["teacher_section"] = current_section
            if form_subject_id:
                selected_subject_value = form_subject_id
            if not (current_branch_id and current_section and selected_subject_value and period):
                flash("Select branch, section, subject, and period before saving attendance.", "error")
                return redirect(url_for("teacher_mark_attendance", branch_id=current_branch_id, section=current_section, subject_id=selected_subject_value, period=period, date=selected_date))

            timetable_context = _resolve_timetable_slots(
                db,
                current_branch_id,
                selected_subject_value,
                selected_date,
                section=current_section,
                period=period,
            )
            selected_period = timetable_context.get("selected_period")
            if not selected_period:
                flash("No timetable period matches the selected branch, section, subject, and period.", "error")
                return redirect(url_for("teacher_mark_attendance", branch_id=current_branch_id, section=current_section, subject_id=selected_subject_value, period=period, date=selected_date))

            subject_id = selected_period.get("subject_id") or subject_id
            subject_name = selected_period.get("subject_name") or subject_name

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

                        ok_student = db.execute(
                            f"SELECT 1 FROM students WHERE id = {placeholder} AND branch_id = {placeholder} AND (COALESCE(section, '') = COALESCE({placeholder}, '') OR COALESCE(section, '') = '')",
                            (student_id, current_branch_id, current_section),
                        ).fetchone()
                        if not ok_student:
                            invalid_students += 1
                            continue

                        subject_clause, subject_params = _attendance_subject_clause(placeholder, subject_id, subject_name)
                        existing = None
                        if subject_clause != "1=0":
                            existing = db.execute(
                                f"""
                                SELECT id, teacher_id
                                FROM attendance
                                WHERE student_id = {placeholder}
                                  AND {subject_clause}
                                  AND date = {placeholder}
                                  AND period = {placeholder}
                                """,
                                tuple([student_id] + subject_params + [selected_date, period]),
                            ).fetchone()
                        existing_teacher_id = row_get(existing, "teacher_id") if existing else None
                        if existing_teacher_id and str(existing_teacher_id) != str(teacher["teacher_id"]):
                            blocked_overwrites += 1
                            continue

                        if existing:
                            db.execute(
                                f"""
                                UPDATE attendance
                                SET status = {placeholder},
                                    note = {placeholder},
                                    teacher_id = {placeholder},
                                    subject_name = {placeholder},
                                    branch_section = {placeholder},
                                    section = {placeholder}
                                WHERE id = {placeholder}
                                """,
                                (status, note, teacher["teacher_id"], subject_name, current_section, current_section, row_get(existing, "id")),
                            )
                        else:
                            db.execute(
                                f"""
                                INSERT INTO attendance (
                                    student_id, branch_id, branch_section, section, subject_id, teacher_id, subject_name,
                                    date, period, status, note
                                ) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
                                """,
                                (student_id, current_branch_id, current_section, current_section, subject_id, teacher["teacher_id"], subject_name, selected_date, period, status, note),
                            )
                        if str(student_id).isdigit():
                            saved_ids.append(int(student_id))
                    db.commit()
                    if invalid_students:
                        flash(f"Skipped {invalid_students} invalid student(s).", "warning")
                    if blocked_overwrites:
                        flash(f"Skipped {blocked_overwrites} record(s) already owned by another teacher.", "warning")
                    flash(f"Attendance for Period {period} saved successfully.", "success")
                    return redirect(url_for("teacher_mark_attendance", branch_id=current_branch_id, section=current_section, subject_id=selected_subject_value, period=period, date=selected_date))
                except Exception as save_error:
                    db.rollback()
                    print(f"[teacher_mark_attendance] ERROR: {repr(save_error)}")
                    flash("Failed to save attendance.", "error")

        if not students and current_branch_id and current_section and selected_subject_value and period:
            students = db.execute(
                f"SELECT id, name, enrollment, roll_no, section FROM students WHERE branch_id = {placeholder} AND (COALESCE(section, '') = COALESCE({placeholder}, '') OR COALESCE(section, '') = '') ORDER BY COALESCE(import_order, id), id",
                (current_branch_id, current_section),
            ).fetchall()

        if not attendance_map and current_branch_id and period:
            subject_clause, subject_params = _attendance_subject_clause(placeholder, subject_id, subject_name)
            if subject_clause != "1=0":
                for row in db.execute(
                    f"""
                    SELECT student_id, status, note
                    FROM attendance
                    WHERE branch_id = {placeholder}
                      AND {subject_clause}
                      AND date = {placeholder}
                      AND period = {placeholder}
                    """,
                    tuple([current_branch_id] + subject_params + [selected_date, period]),
                ).fetchall():
                    attendance_map[str(row_get(row, "student_id"))] = row

        return render_template(
            "teacher_mark_attendance.html",
            teacher=teacher,
            subjects=branch_subjects,
            sections=branch_sections,
            periods=timetable_context.get("slots", []),
            today_slots=today_context.get("schedule_rows", []),
            students=students,
            attendance_map=attendance_map,
            selected_date=selected_date,
            period=period,
            today_date=today_str,
            current_section=current_section,
            selected_subject_id=selected_subject_value,
            selected_subject_name=selected_subject_name,
            selected_period=selected_period,
            current_active_period=current_active_period,
            selected_branch_id=current_branch_id,
            selected_branch_name=current_branch_name,
            can_mark_attendance=can_mark_attendance,
            schedule_message=schedule_message,
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

        subject_stats = []
        for sub in subjects:
            sub_id = row_get(sub, "id")
            sub_records = [r for r in attendance_records if row_get(r, "subject_id") == sub_id]
            sub_total = len(sub_records)
            sub_present = len([r for r in sub_records if row_get(r, "status") == "Present"])
            sub_pct = round((sub_present / sub_total) * 100, 1) if sub_total > 0 else 0
            subject_stats.append({"name": row_get(sub, "name"), "percentage": sub_pct})

        subject_chart_labels = [s["name"] for s in subject_stats]
        subject_chart_percentages = [s["percentage"] for s in subject_stats]

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
        return render_template("student_dashboard.html", student=student, attendance_records=attendance_records, total_classes=total, present_count=present, absent_count=absent, percentage=percentage, subjects=subjects, subject_chart_labels=subject_chart_labels, subject_chart_percentages=subject_chart_percentages, selected_subject_id=selected_subject_id, student_qr_data_uri=student_qr_data_uri)
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


def _attendance_pick_time(time_override: str = "") -> str:
    if time_override:
        try:
            return datetime.fromisoformat(time_override).strftime("%H:%M")
        except Exception:
            pass
    return datetime.now().strftime("%H:%M")


def _attendance_parse_clock(value):
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for parser in (
        lambda raw: datetime.fromisoformat(raw).time(),
        lambda raw: datetime.strptime(raw, "%H:%M").time(),
        lambda raw: datetime.strptime(raw, "%H:%M:%S").time(),
    ):
        try:
            return parser(text)
        except Exception:
            continue
    return None


def _attendance_format_clock(value):
    parsed = _attendance_parse_clock(value)
    if not parsed:
        return str(value or "").strip()
    return datetime.combine(date.today(), parsed).strftime("%I:%M %p")


def _attendance_datetime_for_day(day_value, time_value):
    parsed_time = _attendance_parse_clock(time_value)
    if not parsed_time:
        return None
    return datetime.combine(day_value, parsed_time)


def _ensure_attendance_schema(db):
    try:
        cols = {name.lower() for name in _table_columns(db, "attendance")}
    except Exception:
        cols = set()
    if not cols:
        return
    try:
        if "branch_section" not in cols:
            db.execute("ALTER TABLE attendance ADD COLUMN branch_section TEXT")
        if "section" not in cols:
            db.execute("ALTER TABLE attendance ADD COLUMN section TEXT")
        if "subject_name" not in cols:
            db.execute("ALTER TABLE attendance ADD COLUMN subject_name TEXT")
        if "period" not in cols:
            db.execute("ALTER TABLE attendance ADD COLUMN period TEXT")
        if "teacher_id" not in cols:
            db.execute("ALTER TABLE attendance ADD COLUMN teacher_id INTEGER")
        if "marked_at" not in cols:
            db.execute("ALTER TABLE attendance ADD COLUMN marked_at TEXT DEFAULT CURRENT_TIMESTAMP")
        try:
            db.execute("DROP INDEX IF EXISTS idx_attendance_student_subject_date")
        except Exception:
            pass
        try:
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_student_subject_date_period ON attendance(student_id, subject_id, date, period)")
        except Exception:
            pass
        try:
            db.commit()
        except Exception:
            pass
    except Exception:
        print("[schema] attendance fallback column initialization skipped")


def _resolve_attendance_periods(db, branch_id="", subject_id="", selected_date=None, section="", time_override=""):
    selected_date_obj = selected_date or date.today()
    if isinstance(selected_date_obj, str):
        try:
            selected_date_obj = date.fromisoformat(selected_date_obj)
        except Exception:
            selected_date_obj = date.today()
    weekday = selected_date_obj.strftime("%A")
    current_time = _attendance_pick_time(time_override)
    branch_id_val = _coerce_int(branch_id)
    subject_id_val = _coerce_int(subject_id)
    section_val = (section or "").strip()
    placeholder = get_placeholder()

    base_sql = (
        "SELECT te.*, COALESCE(s.name, te.subject_name, '') AS subject_name, COALESCE(t.name, te.faculty_name, '') AS faculty_name, "
        "COALESCE(b.name, '') AS branch_name "
        "FROM timetable_entries te "
        "LEFT JOIN subjects s ON te.subject_id = s.id "
        "LEFT JOIN teachers t ON te.teacher_id = t.id "
        "LEFT JOIN branches b ON te.branch_id = b.id "
        "WHERE LOWER(TRIM(COALESCE(te.day, ''))) = LOWER(TRIM({}))"
    ).format(placeholder)
    params = [weekday]
    if branch_id_val is not None:
        base_sql += f" AND te.branch_id = {placeholder}"
        params.append(branch_id_val)
    if subject_id_val is not None:
        base_sql += f" AND te.subject_id = {placeholder}"
        params.append(subject_id_val)
    if section_val:
        base_sql += f" AND LOWER(TRIM(COALESCE(te.section, ''))) = LOWER(TRIM({placeholder}))"
        params.append(section_val)
    base_sql += " ORDER BY te.start_time, te.end_time, te.id"

    rows = []
    source = "normalized"
    try:
        rows = db.execute(base_sql, tuple(params)).fetchall()
    except Exception as e:
        print(f"[attendance] timetable_entries lookup failed: {repr(e)}")
        rows = []

    if not rows:
        source = "legacy"
        legacy_sql = (
            "SELECT ts.*, COALESCE(ts.subject_name, '') AS subject_name, COALESCE(ts.faculty_name, '') AS faculty_name, "
            "COALESCE(ts.branch, '') AS branch_name FROM timetable_slots ts "
            "WHERE LOWER(TRIM(COALESCE(ts.day, ''))) = LOWER(TRIM({}))"
        ).format(placeholder)
        legacy_params = [weekday]
        branch_name = ""
        if branch_id_val is not None:
            try:
                branch_row = db.execute(
                    f"SELECT name FROM branches WHERE id = {placeholder}",
                    (branch_id_val,),
                ).fetchone()
                branch_name = row_get(branch_row, "name", "") or ""
            except Exception:
                branch_name = ""
            if branch_name:
                legacy_sql += f" AND LOWER(TRIM(COALESCE(ts.branch, ''))) = LOWER(TRIM({placeholder}))"
                legacy_params.append(branch_name)
        if section_val:
            legacy_sql += f" AND LOWER(TRIM(COALESCE(ts.section, ''))) = LOWER(TRIM({placeholder}))"
            legacy_params.append(section_val)
        if subject_id_val is not None:
            try:
                subject_row = db.execute(
                    f"SELECT name FROM subjects WHERE id = {placeholder}",
                    (subject_id_val,),
                ).fetchone()
                subject_name = row_get(subject_row, "name", "") or ""
            except Exception:
                subject_name = ""
            if subject_name:
                legacy_sql += f" AND LOWER(TRIM(COALESCE(ts.subject_name, ''))) = LOWER(TRIM({placeholder}))"
                legacy_params.append(subject_name)
        legacy_sql += " ORDER BY ts.start_time, ts.end_time"
        try:
            rows = db.execute(legacy_sql, tuple(legacy_params)).fetchall()
        except Exception as e:
            print(f"[attendance] timetable_slots lookup failed: {repr(e)}")
            rows = []

    # Build deduped periods list; skip empty or malformed rows
    periods = []
    active_index = None
    selected_index = None
    is_today = selected_date_obj == date.today()
    seen = set()
    for idx, row in enumerate(rows, start=1):
        start_time = (row_get(row, "start_time") or "").strip()
        end_time = (row_get(row, "end_time") or "").strip()
        # skip empty time slots
        if not start_time or not end_time:
            continue

        subject_name = (row_get(row, "subject_name") or "").strip().lower()
        branch_name = (row_get(row, "branch_name") or "").strip().lower()
        section_name = (row_get(row, "section") or section_val or "").strip().lower()
        room = (row_get(row, "room") or "").strip().lower()
        faculty = (row_get(row, "faculty_name") or row_get(row, "teacher_name") or "").strip().lower()

        key = (start_time, end_time, subject_name, branch_name, section_name, room, faculty)
        if key in seen:
            # skip duplicate timetable rows
            continue
        seen.add(key)

        is_active = bool(is_today and start_time <= current_time <= end_time)
        if is_active and active_index is None:
            active_index = len(periods)

        periods.append({
            "period": len(periods) + 1,
            "timetable_entry_id": row_get(row, "id"),
            "branch_id": row_get(row, "branch_id") or branch_id_val,
            "branch_name": row_get(row, "branch_name") or "",
            "section": row_get(row, "section") or section_val,
            "subject_id": row_get(row, "subject_id") or subject_id_val,
            "subject_name": row_get(row, "subject_name") or "",
            "faculty": row_get(row, "faculty_name") or row_get(row, "teacher_name") or "",
            "room": row_get(row, "room") or "",
            "day": row_get(row, "day") or weekday,
            "start_time": start_time,
            "end_time": end_time,
            "is_lab": bool(row_get(row, "is_lab", 0)),
            "is_active": is_active,
            "status_label": "Current Active Class" if is_active else "Scheduled Class",
            "source": source,
        })

    # Auto-select rules:
    # 1) If there is an active period, select it.
    # 2) If only one period matches the filters, select it (helpful for single-slot days).
    # 3) Otherwise on non-today dates pre-select the first period.
    if active_index is not None:
        selected_index = active_index
    elif len(periods) == 1:
        selected_index = 0
    elif periods and not is_today:
        selected_index = 0

    selected_period = periods[selected_index] if selected_index is not None and selected_index < len(periods) else None
    active_period = periods[active_index] if active_index is not None and active_index < len(periods) else None

    return {
        "periods": periods,
        "selected_period": selected_period,
        "active_period": active_period,
        "has_schedule": bool(periods),
        "is_today": is_today,
        "current_time": current_time,
        "weekday": weekday,
        "source": source,
    }


def _resolve_timetable_slots(db, branch_id="", subject_id="", selected_date=None, section="", period="", time_override="", timetable_entry_id="", grace_minutes=None):
    selected_date_obj = selected_date or date.today()
    if isinstance(selected_date_obj, str):
        try:
            selected_date_obj = date.fromisoformat(selected_date_obj)
        except Exception:
            selected_date_obj = date.today()

    current_dt = datetime.now()
    if time_override:
        try:
            current_dt = datetime.combine(selected_date_obj, _attendance_parse_clock(time_override) or current_dt.time())
        except Exception:
            pass

    current_time = current_dt.strftime("%H:%M")
    weekday = selected_date_obj.strftime("%A")
    weekday_short = selected_date_obj.strftime("%a")
    is_today = selected_date_obj == date.today()
    if grace_minutes is None:
        try:
            grace_minutes = int(os.environ.get("ATTENDANCE_GRACE_MINUTES", "15"))
        except Exception:
            grace_minutes = 15
    grace_delta = timedelta(minutes=grace_minutes)
    placeholder = get_placeholder()

    selected_branch_id, selected_branch_name, section_hint = _resolve_timetable_branch_lookup(db, branch_id, section=section)

    if selected_branch_id is None:
        reason = _attendance_no_schedule_reason(db, branch_id, section=section, weekday=weekday)
        print(f"[attendance] selected branch_id={branch_id} section={section!r} weekday={weekday} rows=0 subjects=0 reason={reason}")
        return {
            "schedule_rows": [],
            "periods": [],
            "slots": [],
            "selected_slot": None,
            "selected_period": None,
            "active_slot": None,
            "active_period": None,
            "next_slot": None,
            "remaining_slots": [],
            "manual_slots": [],
            "has_schedule": False,
            "is_today": is_today,
            "current_time": current_time,
            "current_time_label": current_dt.strftime("%I:%M %p"),
            "weekday": weekday,
            "weekday_short": weekday_short,
            "grace_minutes": grace_minutes,
            "can_mark_attendance": False,
            "schedule_message": reason or "No class is currently running.",
            "current_class_message": reason or "No class is currently running.",
            "next_class_message": "",
            "selected_branch_id": None,
            "selected_branch_name": selected_branch_name,
            "selected_section": (section or "").strip(),
            "selected_timetable_entry_id": "",
            "selected_subject_id": None,
            "selected_subject_name": "",
            "selected_period_number": "",
            "selected_teacher_id": None,
            "selected_teacher_name": "",
            "unique_slot": False,
        }

    # Normalize section input (accept CSM-A, CSM A, A etc.)
    section_val = _normalize_attendance_section_input(section_hint or section)
    if not section_val and selected_branch_name:
        _, derived_section = split_branch_section(selected_branch_name)
        section_val = _normalize_attendance_section_input(derived_section)

    # If still no explicit section, try to pick a default if only one exists
    if not section_val:
        try:
            branch_sections = _get_timetable_sections_for_branch(db, selected_branch_id)
        except Exception:
            branch_sections = []
        if len(branch_sections) == 1:
            section_val = _normalize_attendance_section_input(branch_sections[0])

    # Require an explicit section for deterministic timetable resolution
    if not section_val:
        print(f"[attendance] selected branch_id={selected_branch_id} section='' weekday={weekday} rows=0 subjects=0 reason=Section mismatch")
        return {
            "schedule_rows": [],
            "periods": [],
            "slots": [],
            "selected_slot": None,
            "selected_period": None,
            "active_slot": None,
            "active_period": None,
            "next_slot": None,
            "remaining_slots": [],
            "manual_slots": [],
            "has_schedule": False,
            "is_today": is_today,
            "current_time": current_time,
            "current_time_label": current_dt.strftime("%I:%M %p"),
            "weekday": weekday,
            "weekday_short": weekday_short,
            "grace_minutes": grace_minutes,
            "can_mark_attendance": False,
            "schedule_message": "Select a section to load today's timetable.",
            "current_class_message": "No active class at the current time.",
            "next_class_message": "",
            "selected_branch_id": selected_branch_id,
            "selected_branch_name": selected_branch_name,
            "selected_section": "",
            "selected_timetable_entry_id": "",
            "selected_subject_id": None,
            "selected_subject_name": "",
            "selected_period_number": "",
            "selected_teacher_id": None,
            "selected_teacher_name": "",
            "unique_slot": False,
        }

    req_subject_id = _coerce_int(subject_id)
    req_subject_name = ""
    if subject_id and not str(subject_id).strip().isdigit():
        req_subject_name = str(subject_id).strip()

    rows = []
    try:
        weekday_short = selected_date_obj.strftime("%a").upper()
        weekday_full = selected_date_obj.strftime("%A").upper()
        sql = (
            "SELECT te.*, COALESCE(te.subject_name, '') AS subject_name_db, COALESCE(te.faculty_name, '') AS teacher_name_db, COALESCE(b.name, '') AS branch_name_db "
            "FROM timetable_entries te "
            "LEFT JOIN branches b ON te.branch_id = b.id "
            f"WHERE te.branch_id = {placeholder} "
            f"AND (LOWER(TRIM(te.day)) = LOWER({placeholder}) OR LOWER(TRIM(te.day)) = LOWER({placeholder}))"
        )
        params = [selected_branch_id, weekday_short, weekday_full]
        if section_val:
            combined_sec = f"{selected_branch_name}-{section_val}"
            sql += f" AND (LOWER(TRIM(COALESCE(te.section, ''))) = LOWER(TRIM({placeholder})) OR LOWER(TRIM(COALESCE(te.section, ''))) = LOWER(TRIM({placeholder})))"
            params.extend([section_val, combined_sec])
        if req_subject_id is not None:
            sql += f" AND (te.subject_id = {placeholder} OR LOWER(TRIM(COALESCE(te.subject_name, ''))) = LOWER(TRIM({placeholder})))"
            params.extend([req_subject_id, req_subject_name or str(req_subject_id)])
        elif req_subject_name:
            sql += f" AND LOWER(TRIM(COALESCE(te.subject_name, ''))) = LOWER(TRIM({placeholder}))"
            params.append(req_subject_name)
        sql += " ORDER BY te.start_time, te.end_time, te.id"
        rows = db.execute(sql, tuple(params)).fetchall()
    except Exception as e:
        print(f"[attendance] timetable_entries lookup failed: {repr(e)}")
        rows = []

    print(f"[attendance] timetable_entries lookup branch_id={selected_branch_id} section={section_val!r} weekday={weekday} subject={subject_id!r} rows={len(rows)}")

    if not rows:
        reason = _attendance_no_schedule_reason(db, selected_branch_id, section=section_val, weekday=weekday)
        print(f"[attendance] timetable_entries empty branch_id={selected_branch_id} section={section_val!r} weekday={weekday} reason={reason}")
        return {
            "schedule_rows": [],
            "periods": [],
            "slots": [],
            "selected_slot": None,
            "selected_period": None,
            "active_slot": None,
            "active_period": None,
            "next_slot": None,
            "remaining_slots": [],
            "manual_slots": [],
            "has_schedule": False,
            "is_today": is_today,
            "current_time": current_time,
            "current_time_label": current_dt.strftime("%I:%M %p"),
            "weekday": weekday,
            "weekday_short": weekday_short,
            "grace_minutes": grace_minutes,
            "can_mark_attendance": False,
            "schedule_message": reason or "No class is currently running.",
            "current_class_message": reason or "No class is currently running.",
            "next_class_message": "",
            "selected_branch_id": selected_branch_id,
            "selected_branch_name": selected_branch_name,
            "selected_section": section_val,
            "selected_timetable_entry_id": "",
            "selected_subject_id": None,
            "selected_subject_name": "",
            "selected_period_number": "",
            "selected_teacher_id": None,
            "selected_teacher_name": "",
            "unique_slot": False,
        }

    schedule_rows = []
    seen = set()
    for row in rows:
        day_name = row_get(row, "day") or ""
        if not (day_matches(day_name, weekday) or day_matches(day_name, weekday_short)):
            continue
        start_time = (row_get(row, "start_time") or "").strip()
        end_time = (row_get(row, "end_time") or "").strip()
        if not start_time or not end_time:
            continue
        start_dt = _attendance_datetime_for_day(selected_date_obj, start_time)
        end_dt = _attendance_datetime_for_day(selected_date_obj, end_time)
        if not start_dt or not end_dt:
            continue
        subject_name = (row_get(row, "subject_name_db") or row_get(row, "subject_name") or "").strip()
        faculty_name = (row_get(row, "teacher_name_db") or row_get(row, "faculty_name") or row_get(row, "teacher_name") or "").strip()
        branch_name = (row_get(row, "branch_name_db") or row_get(row, "branch_name") or selected_branch_name or "").strip()
        section_name = (row_get(row, "section") or section_val or "").strip()
        room = (row_get(row, "room") or "").strip()
        dedupe_key = (start_time, end_time, normalize_text(subject_name), normalize_text(faculty_name), normalize_text(branch_name), normalize_text(section_name), normalize_text(room))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        schedule_rows.append({
            "period": len(schedule_rows) + 1,
            "timetable_entry_id": row_get(row, "id"),
            "branch_id": row_get(row, "branch_id") or selected_branch_id,
            "branch_name": branch_name,
            "section": section_name,
            "subject_id": row_get(row, "subject_id"),
            "subject_name": subject_name,
            "teacher_id": row_get(row, "teacher_id"),
            "teacher_name": faculty_name,
            "faculty_name": faculty_name,
            "faculty": faculty_name,
            "room": room,
            "day": day_name or weekday,
            "start_time": start_time,
            "end_time": end_time,
            "start_time_label": _attendance_format_clock(start_time),
            "end_time_label": _attendance_format_clock(end_time),
            "start_dt": start_dt,
            "end_dt": end_dt,
            "is_lab": bool(row_get(row, "is_lab", 0)),
            "is_active": bool(is_today and start_dt <= current_dt <= end_dt),
            "source": "normalized",
        })

    schedule_rows.sort(key=lambda item: (item["start_dt"], item["end_dt"], item["timetable_entry_id"] or 0))

    active_index = None
    next_index = None
    explicit_index = None
    period_index = None
    for idx, slot in enumerate(schedule_rows):
        if slot["is_active"] and active_index is None:
            active_index = idx
        if is_today and next_index is None and slot["start_dt"] > current_dt:
            next_index = idx
        if timetable_entry_id and str(slot["timetable_entry_id"]) == str(timetable_entry_id):
            explicit_index = idx
        if period and str(slot.get("period")) == str(period):
            period_index = idx

    selected_index = None
    if active_index is not None:
        selected_index = active_index
    elif explicit_index is not None:
        selected_index = explicit_index
    elif period_index is not None:
        selected_index = period_index
    elif is_today and next_index is not None:
        selected_index = next_index
    elif schedule_rows:
        selected_index = 0

    selected_slot = schedule_rows[selected_index] if selected_index is not None and selected_index < len(schedule_rows) else None
    active_slot = schedule_rows[active_index] if active_index is not None and active_index < len(schedule_rows) else None
    next_slot = schedule_rows[next_index] if next_index is not None and next_index < len(schedule_rows) else None

    subjects_sorted = sorted({slot["subject_name"] for slot in schedule_rows if slot.get("subject_name")})
    print(
        f"[attendance] timetable periods branch_id={selected_branch_id} section={section_val!r} weekday={weekday} periods={len(schedule_rows)} subjects={subjects_sorted}"
    )

    remaining_slots = []
    if schedule_rows:
        if active_index is not None:
            remaining_slots = schedule_rows[active_index + 1 :]
        elif next_index is not None:
            remaining_slots = schedule_rows[next_index:]
        else:
            remaining_slots = schedule_rows[:]

    can_mark_attendance = False
    if selected_slot and is_today:
        if active_slot and str(active_slot.get("timetable_entry_id")) == str(selected_slot.get("timetable_entry_id")):
            can_mark_attendance = True
        else:
            grace_limit = selected_slot["start_dt"] + grace_delta
            can_mark_attendance = selected_slot["start_dt"] <= current_dt <= grace_limit

    current_class_message = "No class is currently running."
    if active_slot:
        current_class_message = f"Current Class: {active_slot['subject_name']} (Period {active_slot['period']})"

    next_class_message = ""
    if next_slot:
        next_class_message = f"Next Class starts at {next_slot['start_time_label']}."
    elif is_today and schedule_rows and not active_slot:
        next_class_message = "No more classes remain for today."

    if active_slot:
        schedule_message = current_class_message
    elif next_class_message:
        schedule_message = f"{current_class_message} {next_class_message}".strip()
    else:
        schedule_message = current_class_message

    print(f"Selected branch: {selected_branch_name} (ID: {selected_branch_id})")
    print(f"Selected section: {section_val}")
    print(f"Selected weekday: {weekday}")
    print(f"Timetable rows found: {len(schedule_rows)}")
    print(f"Subjects returned: {[s.get('subject_name') for s in schedule_rows]}")
    print(f"Active period detected: {active_slot['period'] if active_slot else 'None'}")

    def _public_slot(slot):
        if not slot:
            return None
        public = dict(slot)
        public.pop("start_dt", None)
        public.pop("end_dt", None)
        return public

    public_rows = [_public_slot(slot) for slot in schedule_rows]
    public_selected = _public_slot(selected_slot)
    public_active = _public_slot(active_slot)
    public_next = _public_slot(next_slot)
    public_remaining = [_public_slot(slot) for slot in remaining_slots]

    return {
        "schedule_rows": public_rows,
        "periods": public_rows,
        "slots": public_rows,
        "selected_slot": public_selected,
        "selected_period": public_selected,
        "active_slot": public_active,
        "active_period": public_active,
        "next_slot": public_next,
        "remaining_slots": public_remaining,
        "manual_slots": public_remaining,
        "has_schedule": bool(public_rows),
        "is_today": is_today,
        "current_time": current_time,
        "current_time_label": current_dt.strftime("%I:%M %p"),
        "weekday": weekday,
        "weekday_short": weekday_short,
        "grace_minutes": grace_minutes,
        "can_mark_attendance": can_mark_attendance,
        "schedule_message": schedule_message,
        "current_class_message": current_class_message,
        "next_class_message": next_class_message,
        "selected_branch_id": selected_branch_id,
        "selected_branch_name": selected_branch_name,
        "selected_section": section_val,
        "selected_timetable_entry_id": str(selected_slot["timetable_entry_id"]) if selected_slot else "",
        "selected_subject_id": selected_slot["subject_id"] if selected_slot else None,
        "selected_subject_name": selected_slot["subject_name"] if selected_slot else "",
        "selected_period_number": selected_slot["period"] if selected_slot else "",
        "selected_teacher_id": selected_slot["teacher_id"] if selected_slot else None,
        "selected_teacher_name": selected_slot["teacher_name"] if selected_slot else "",
        "unique_slot": len(public_rows) == 1,
        "source": "normalized",
    }


def _attendance_students_for_branch(db, branch_id, section=""):
    placeholder = get_placeholder()
    section_val = (section or "").strip()
    try:
        if section_val:
            return db.execute(
                f"SELECT id, name, enrollment FROM students WHERE branch_id = {placeholder} AND (COALESCE(section,'') = COALESCE({placeholder}, '') OR COALESCE(section,'') = '') ORDER BY COALESCE(import_order, id), id",
                (branch_id, section_val),
            ).fetchall()
    except Exception:
        pass
    return db.execute(
        f"SELECT id, name, enrollment FROM students WHERE branch_id = {placeholder} ORDER BY COALESCE(import_order, id), id",
        (branch_id,),
    ).fetchall()


def _attendance_subject_clause(placeholder, subject_id=None, subject_name=""):
    subject_id_val = "" if subject_id is None else str(subject_id).strip()
    if subject_id_val:
        return f"subject_id = {placeholder}", [subject_id]
    subject_name_val = (subject_name or "").strip()
    if subject_name_val:
        return f"LOWER(TRIM(COALESCE(subject_name, ''))) = LOWER(TRIM({placeholder}))", [subject_name_val]
    return "1=0", []


@app.route("/attendance", methods=["GET", "POST"])
@login_required
def mark_attendance():
    db = get_db()
    placeholder = get_placeholder()
    branches = db.execute("SELECT * FROM branches ORDER BY name").fetchall()
    branch_id = request.values.get("branch_id") or ""
    section = (request.values.get("section") or "").strip()
    selected_date = request.values.get("date") or date.today().isoformat()
    timetable_entry_id = request.values.get("timetable_entry_id") or ""
    subject_id = request.values.get("subject_id") or ""
    period = request.values.get("period") or ""
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
    weekday = selected_date_obj.strftime("%A")
    weekday_short = selected_date_obj.strftime("%a")

    current_date_obj = selected_date_obj
    prev_date = (current_date_obj - timedelta(days=1)).isoformat()
    next_date = (current_date_obj + timedelta(days=1)).isoformat()

    sections = []
    today_context = {
        "schedule_rows": [],
        "slots": [],
        "selected_slot": None,
        "active_slot": None,
        "has_schedule": False,
        "schedule_message": "Select a section to load today's timetable.",
    }
    timetable_context = {
        "schedule_rows": [],
        "periods": [],
        "slots": [],
        "selected_slot": None,
        "selected_period": None,
        "active_slot": None,
        "active_period": None,
        "next_slot": None,
        "remaining_slots": [],
        "manual_slots": [],
        "has_schedule": False,
        "is_today": False,
        "current_time": "",
        "current_time_label": "",
        "weekday": "",
        "weekday_short": "",
        "grace_minutes": 15,
        "can_mark_attendance": False,
        "schedule_message": "",
        "current_class_message": "",
        "next_class_message": "",
        "selected_branch_id": None,
        "selected_branch_name": "",
        "selected_section": section,
        "selected_timetable_entry_id": timetable_entry_id,
        "selected_subject_id": None,
        "selected_subject_name": "",
        "selected_period_number": "",
        "selected_teacher_id": None,
        "selected_teacher_name": "",
        "unique_slot": False,
    }

    no_schedule_reason = ""
    if branch_id:
        try:
            sections = _get_timetable_sections_for_branch(db, branch_id)
        except Exception:
            sections = []
        if not section and sections:
            section = sections[0]

    if branch_id and section:
        try:
            subjects = _get_timetable_subjects_for_branch(
                db,
                branch_id,
                section=section,
                weekday=weekday,
                weekday_short=weekday_short,
            )
        except Exception as subject_error:
            print(f"[attendance] Subject lookup failed: {repr(subject_error)}")
            subjects = []
        try:
            today_context = _resolve_timetable_slots(
                db,
                branch_id,
                "",
                selected_date,
                section=section,
                timetable_entry_id=timetable_entry_id,
            )
        except Exception as exc:
            print(f"[attendance] timetable resolve failed: {repr(exc)}")
        if not today_context.get("has_schedule"):
            no_schedule_reason = _attendance_no_schedule_reason(
                db,
                branch_id,
                section=section,
                weekday=weekday,
            )

    selected_subject_token = (subject_id or "").strip()
    if not selected_subject_token and today_context.get("selected_slot"):
        selected_subject_token = str(
            today_context.get("selected_slot", {}).get("subject_id")
            or today_context.get("selected_slot", {}).get("subject_name")
            or ""
        ).strip()
    if not selected_subject_token and len(subjects) == 1:
        selected_subject_token = str(subjects[0].get("id") or "").strip()

    selected_subject = None
    if selected_subject_token:
        token_norm = selected_subject_token.lower()
        for subject in subjects:
            subject_value = str(subject.get("id") or "").strip()
            subject_name_value = str(subject.get("name") or "").strip()
            if token_norm == subject_value.lower() or token_norm == subject_name_value.lower():
                selected_subject = subject
                break

    selected_subject_value = selected_subject.get("id") if selected_subject else selected_subject_token
    selected_subject_name = selected_subject.get("name") if selected_subject else ""

    if branch_id and section:
        try:
            if selected_subject_value:
                timetable_context = _resolve_timetable_slots(
                    db,
                    branch_id,
                    selected_subject_value,
                    selected_date,
                    section=section,
                    period=period,
                    timetable_entry_id=timetable_entry_id,
                )
            else:
                timetable_context = today_context
        except Exception as exc:
            print(f"[attendance] timetable resolve failed: {repr(exc)}")

    section = timetable_context.get("selected_section") or section
    if timetable_context.get("selected_slot"):
        period = str(timetable_context.get("selected_period_number") or period or "")
        timetable_entry_id = str(timetable_context.get("selected_timetable_entry_id") or timetable_entry_id or "")

    if timetable_context.get("selected_slot") and selected_subject_value:
        students = _attendance_students_for_branch(db, branch_id, section)
        student_count = len(students) if isinstance(students, (list, tuple)) else 0
        print(f"[mark_attendance] Loaded students count={student_count}")
        selected_subject_id = timetable_context.get("selected_subject_id")
        selected_subject_name = selected_subject_name or timetable_context.get("selected_subject_name") or ""
        subject_clause, subject_params = _attendance_subject_clause(placeholder, selected_subject_id, selected_subject_name)
        if subject_clause != "1=0":
            existing_dates = db.execute(
                f"SELECT date, COUNT(*) as count FROM attendance WHERE branch_id = {placeholder} AND {subject_clause} GROUP BY date ORDER BY date DESC",
                tuple([branch_id] + subject_params),
            ).fetchall()

    subject_names = [str(s.get("name") or "").strip() for s in subjects]
    print(
        f"[attendance] selected branch={branch_id} section={section!r} weekday={weekday} timetable_rows={len(today_context.get('schedule_rows', []))} subjects={subject_names} active_period={(today_context.get('active_slot') or {}).get('period') if today_context.get('active_slot') else ''}"
    )

    if request.method == "POST":
        branch_id = request.form.get("branch_id") or ""
        section = (request.form.get("section") or section or "").strip()
        selected_date = request.form.get("date") or date.today().isoformat()
        timetable_entry_id = request.form.get("timetable_entry_id") or timetable_entry_id or ""
        subject_id = request.form.get("subject_id") or subject_id or ""
        period = request.form.get("period") or period or ""
        try:
            selected_date_obj = date.fromisoformat(selected_date)
        except ValueError:
            selected_date_obj = today_date

        if selected_date_obj > today_date:
            selected_date_obj = today_date

        selected_date = selected_date_obj.isoformat()

        sections = []
        if branch_id:
            try:
                sections = _get_timetable_sections_for_branch(db, branch_id)
            except Exception:
                sections = []
            if not section and sections:
                section = sections[0]

        try:
            timetable_context = _resolve_timetable_slots(
                db,
                branch_id,
                subject_id,
                selected_date,
                section=section,
                period=period,
                timetable_entry_id=timetable_entry_id,
            )
        except Exception as exc:
            print(f"[attendance] timetable resolve failed on POST: {repr(exc)}")
            timetable_context = {
                "schedule_rows": [],
                "periods": [],
                "slots": [],
                "selected_slot": None,
                "selected_period": None,
                "active_slot": None,
                "active_period": None,
                "next_slot": None,
                "remaining_slots": [],
                "manual_slots": [],
                "has_schedule": False,
                "is_today": False,
                "current_time": "",
                "current_time_label": "",
                "weekday": "",
                "weekday_short": "",
                "grace_minutes": 15,
                "can_mark_attendance": False,
                "schedule_message": "",
                "current_class_message": "",
                "next_class_message": "",
                "selected_branch_id": None,
                "selected_branch_name": "",
                "selected_section": section,
                "selected_timetable_entry_id": timetable_entry_id,
                "selected_subject_id": None,
                "selected_subject_name": "",
                "selected_period_number": "",
                "selected_teacher_id": None,
                "selected_teacher_name": "",
                "unique_slot": False,
            }
        selected_slot = timetable_context["selected_slot"]
        if not branch_id or not selected_slot:
            flash("Please select a branch and timetable slot.", "error")
        elif not timetable_context["can_mark_attendance"]:
            flash("Attendance can only be marked during the active class or grace period.", "error")
        else:
            student_ids = request.form.getlist("student_id")
            if branch_id and student_ids:
                saved_student_ids = []
                try:
                    selected_subject_id = selected_slot.get("subject_id")
                    selected_subject_name = selected_slot.get("subject_name") or ""
                    selected_teacher_id = selected_slot.get("teacher_id") or session.get("teacher_id")
                    selected_period_number = str(selected_slot.get("period") or "")
                    selected_timetable_entry_id = selected_slot.get("timetable_entry_id")
                    current_section = timetable_context.get("selected_section") or section
                    attendance_timestamp = datetime.now().isoformat(sep=" ", timespec="seconds")
                    for student_id in student_ids:
                        status = request.form.get(f"status_{student_id}", "Absent")
                        note = request.form.get(f"note_{student_id}", "")
                        subject_clause, subject_params = _attendance_subject_clause(placeholder, selected_subject_id, selected_subject_name)
                        existing = None
                        if subject_clause != "1=0":
                            existing = db.execute(
                                f"SELECT id FROM attendance WHERE student_id = {placeholder} AND {subject_clause} AND date = {placeholder} AND period = {placeholder}",
                                tuple([student_id] + subject_params + [selected_date, selected_period_number]),
                            ).fetchone()
                        if existing:
                            db.execute(
                                f"UPDATE attendance SET branch_id = {placeholder}, branch_section = {placeholder}, section = {placeholder}, subject_name = {placeholder}, status = {placeholder}, note = {placeholder}, teacher_id = {placeholder}, marked_at = {placeholder} WHERE id = {placeholder}",
                                (branch_id, current_section, current_section, selected_subject_name, status, note, selected_teacher_id, attendance_timestamp, row_get(existing, "id")),
                            )
                        else:
                            db.execute(
                                f"INSERT INTO attendance (student_id, branch_id, branch_section, section, subject_id, subject_name, period, date, status, note, teacher_id, marked_at) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
                                (student_id, branch_id, current_section, current_section, selected_subject_id, selected_subject_name, selected_period_number, selected_date, status, note, selected_teacher_id, attendance_timestamp),
                            )
                        if student_id.isdigit():
                            saved_student_ids.append(int(student_id))
                    db.commit()
                    flash("Attendance saved successfully.", "success")
                    emailed_students = notify_low_attendance(db, saved_student_ids)
                    session["attendance_email_summary"] = emailed_students
                    db.close()
                    return redirect(
                        url_for(
                            "attendance_success",
                            branch_id=branch_id,
                            subject_id=selected_subject_id or "",
                            date=selected_date,
                            period=selected_period_number,
                            section=current_section,
                        )
                    )
                except Exception as e:
                    db.rollback()
                    print(f"Error saving attendance: {e}")
                    flash("Error saving attendance. Please try again.", "error")
            else:
                flash("Please select a branch, subject, and mark attendance for students.", "error")

    attendance_map = {}
    if branch_id and timetable_context.get("selected_slot"):
        try:
            selected_subject_id = timetable_context.get("selected_subject_id")
            selected_subject_name = timetable_context.get("selected_subject_name") or ""
            subject_clause, subject_params = _attendance_subject_clause(placeholder, selected_subject_id, selected_subject_name)
            if subject_clause != "1=0":
                rows = db.execute(
                    f"SELECT student_id, status, note FROM attendance WHERE branch_id = {placeholder} AND {subject_clause} AND date = {placeholder} AND period = {placeholder}",
                    tuple([branch_id] + subject_params + [selected_date, timetable_context.get("selected_period_number")]),
                ).fetchall()
                attendance_map = {str(row["student_id"]): row for row in rows}
        except Exception:
            attendance_map = {}

    if branch_id and timetable_context["selected_slot"]:
        students = _attendance_students_for_branch(db, branch_id, section)
        if not existing_dates:
            selected_subject_id = timetable_context.get("selected_subject_id")
            selected_subject_name = timetable_context.get("selected_subject_name") or ""
            subject_clause, subject_params = _attendance_subject_clause(placeholder, selected_subject_id, selected_subject_name)
            if subject_clause != "1=0":
                existing_dates = db.execute(
                    f"SELECT date, COUNT(*) as count FROM attendance WHERE branch_id = {placeholder} AND {subject_clause} GROUP BY date ORDER BY date DESC",
                    tuple([branch_id] + subject_params),
                ).fetchall()

    selected_period = timetable_context["selected_slot"]
    active_period = timetable_context["active_slot"]
    can_mark_attendance = bool(timetable_context.get("can_mark_attendance"))
    schedule_message = timetable_context.get("schedule_message") or ""
    selected_subject_id = timetable_context.get("selected_subject_id") or selected_subject_value or ""
    selected_subject_name = (selected_subject_name or timetable_context.get("selected_subject_name") or "").strip()
    subject_id = str(selected_subject_id or subject_id or "")

    db.close()
    return render_template(
        "mark_attendance.html",
        branches=branches,
        sections=sections,
        subjects=subjects,
        periods=timetable_context.get("slots", []),
        today_slots=today_context.get("schedule_rows", []),
        students=students,
        branch_id=branch_id,
        subject_id=subject_id,
        section=section,
        selected_date=selected_date,
        period=period,
        timetable_slots=timetable_context.get("schedule_rows", []),
        selected_period=selected_period,
        current_active_period=active_period,
        schedule_message=schedule_message,
        can_mark_attendance=can_mark_attendance,
        unique_slot=timetable_context.get("unique_slot", False),
        attendance_map=attendance_map,
        existing_dates=existing_dates,
        prev_date=prev_date,
        next_date=next_date,
        today_date=today_date.isoformat(),
        current_time_label=timetable_context.get("current_time_label", ""),
        current_class_message=timetable_context.get("current_class_message", ""),
        next_class_message=timetable_context.get("next_class_message", ""),
        selected_timetable_entry_id=timetable_context.get("selected_timetable_entry_id", ""),
        selected_subject_name=timetable_context.get("selected_subject_name", ""),
        selected_subject_id=timetable_context.get("selected_subject_id", ""),
        selected_teacher_id=timetable_context.get("selected_teacher_id", ""),
        remaining_slots=timetable_context.get("remaining_slots", []),
        has_schedule=timetable_context.get("has_schedule", False),
        weekday=timetable_context.get("weekday", ""),
        selected_branch_name=timetable_context.get("selected_branch_name", ""),
        selected_section=timetable_context.get("selected_section", section),
    )


@app.route("/api/timetable-sections")
@safe_api
@login_required
def api_timetable_sections():
    branch_id = request.args.get("branch_id") or ""
    db = None
    try:
        db = get_db()
        sections = _get_timetable_sections_for_branch(db, branch_id) if branch_id else []
        return jsonify({"sections": sections, "count": len(sections)})
    except Exception as e:
        return jsonify({"sections": [], "count": 0, "error": str(e)})
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@app.route("/api/current-period")
@safe_api
@login_required
def api_current_period():
    """Return the current/next period matching date/section/subject and auto-return students.

    Query params:
      - date (YYYY-MM-DD) optional, defaults to today
      - section optional
      - subject optional (id or name)
      - time optional (HH:MM) override current time for testing
    """
    selected_date = request.args.get("date") or date.today().isoformat()
    section = (request.args.get("section") or "").strip()
    subject_q = (request.args.get("subject") or "").strip()
    time_override = (request.args.get("time") or "").strip()

    try:
        sel_date_obj = date.fromisoformat(selected_date)
    except Exception:
        sel_date_obj = date.today()

    day = sel_date_obj.strftime("%a")
    now_time = datetime.now().strftime("%H:%M")
    if time_override:
        try:
            now_time = datetime.fromisoformat(time_override).strftime("%H:%M")
        except Exception:
            pass

    db = get_db()
    placeholder = get_placeholder()

    # Try to find an active slot (start_time <= now <= end_time)
    sql = (
        f"SELECT te.*, COALESCE(te.subject_name, '') AS subject_name, COALESCE(t.name, te.faculty_name, '') AS teacher_name, te.branch_id "
        f"FROM timetable_entries te LEFT JOIN teachers t ON te.teacher_id = t.id "
        f"WHERE LOWER(TRIM(COALESCE(te.day,''))) = LOWER(TRIM({placeholder})) AND te.start_time <= {placeholder} AND te.end_time >= {placeholder}"
    )
    params = [day, now_time, now_time]
    if section:
        sql += f" AND LOWER(TRIM(COALESCE(te.section,''))) = LOWER(TRIM({placeholder}))"
        params.append(section)
    if subject_q:
        if subject_q.isdigit():
            sql += f" AND te.subject_id = {placeholder}"
            params.append(int(subject_q))
        else:
            sql += f" AND LOWER(TRIM(COALESCE(te.subject_name, ''))) = LOWER(TRIM({placeholder}))"
            params.append(subject_q)
    sql += " ORDER BY te.start_time LIMIT 1"

    row = db.execute(sql, tuple(params)).fetchone()

    # Fallback: if no active slot, find the next upcoming slot today matching filters
    if not row:
        sql2 = (
            f"SELECT te.*, COALESCE(te.subject_name, '') AS subject_name, COALESCE(t.name, te.faculty_name, '') AS teacher_name, te.branch_id "
            f"FROM timetable_entries te LEFT JOIN teachers t ON te.teacher_id = t.id "
            f"WHERE LOWER(TRIM(COALESCE(te.day,''))) = LOWER(TRIM({placeholder}))"
        )
        params2 = [day]
        if section:
            sql2 += f" AND LOWER(TRIM(COALESCE(te.section,''))) = LOWER(TRIM({placeholder}))"
            params2.append(section)
        if subject_q:
            if subject_q.isdigit():
                sql2 += f" AND te.subject_id = {placeholder}"
                params2.append(int(subject_q))
            else:
                sql2 += f" AND LOWER(TRIM(COALESCE(te.subject_name, ''))) = LOWER(TRIM({placeholder}))"
                params2.append(subject_q)
        sql2 += f" AND te.start_time >= {placeholder} ORDER BY te.start_time LIMIT 1"
        params2.append(now_time)
        row = db.execute(sql2, tuple(params2)).fetchone()

    if not row:
        db.close()
        return jsonify({"error": "no_matching_period"}), 404

    subject_id = row_get(row, "subject_id")
    branch_id = row_get(row, "branch_id")
    start_time = row_get(row, "start_time") or ""
    end_time = row_get(row, "end_time") or ""
    faculty = row_get(row, "teacher_name") or row_get(row, "faculty_name") or ""
    section_val = row_get(row, "section") or section or ""

    # Prevent duplicate attendance sessions: check if attendance rows exist for this subject/date
    try:
        subject_clause, subject_params = _attendance_subject_clause(placeholder, subject_id, row_get(row, "subject_name") or "")
        existing = db.execute(
            f"SELECT COUNT(*) AS c FROM attendance WHERE {subject_clause} AND date = {placeholder}",
            tuple(subject_params + [selected_date]),
        ).fetchone()
        already_marked = int(row_get(existing, "c", 0) or 0) > 0
    except Exception:
        already_marked = False

    # Load students for the section: prefer section filter if students table has section column
    students = []
    try:
        students = db.execute(
            f"SELECT id, name, enrollment FROM students WHERE branch_id = {placeholder} AND (COALESCE(section,'') = COALESCE({placeholder}, '') OR COALESCE(section,'') = '') ORDER BY COALESCE(import_order, id), id",
            (branch_id, section_val),
        ).fetchall()
    except Exception:
        # Fallback to branch-only selection if `section` column is not present
        students = db.execute(
            f"SELECT id, name, enrollment FROM students WHERE branch_id = {placeholder} ORDER BY COALESCE(import_order, id), id",
            (branch_id,),
        ).fetchall()

    student_list = [{"id": r["id"], "name": r["name"], "enrollment": row_get(r, "enrollment")} for r in students]

    resp = {
        "subject": row_get(row, "subject_name") or "",
        "section": section_val,
        "faculty": faculty,
        "start_time": start_time,
        "end_time": end_time,
        # Extra fields helpful to the frontend
        "branch_id": branch_id,
        "subject_id": subject_id,
        "already_marked": already_marked,
        "students": student_list,
    }

    db.close()
    return jsonify(resp)


@app.route("/api/timetable-subjects")
@safe_api
@login_required
def api_timetable_subjects():
    branch_id = request.args.get("branch_id") or ""
    branch_name = (request.args.get("branch_name") or request.args.get("branch") or "").strip()
    branch_section = (request.args.get("branch_section") or request.args.get("section") or "").strip()
    section = (request.args.get("section") or "").strip()
    selected_date = request.args.get("date") or date.today().isoformat()
    db = None
    try:
        db = get_db()
        try:
            selected_date_obj = date.fromisoformat(selected_date)
        except Exception:
            selected_date_obj = date.today()
        weekday = selected_date_obj.strftime("%A")
        weekday_short = selected_date_obj.strftime("%a")
        branch_lookup = branch_id or branch_name
        section_lookup = branch_section or section
        subjects = (
            _get_timetable_subjects_for_branch(
                db,
                branch_lookup,
                section=section_lookup,
                weekday=weekday,
                weekday_short=weekday_short,
            )
            if branch_lookup
            else []
        )
        reason = "" if subjects else _attendance_no_schedule_reason(db, branch_lookup, section=section_lookup, weekday=weekday)
        print(
            f"[api_timetable_subjects] branch={branch_lookup} section={section_lookup!r} weekday={weekday} subjects={len(subjects)} reason={reason}"
        )
        return jsonify({"subjects": subjects, "count": len(subjects), "weekday": weekday, "reason": reason})
    except Exception as e:
        return jsonify({"subjects": [], "count": 0, "error": str(e)})
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@app.route("/api/timetable-slots")
@safe_api
@login_required
def api_timetable_slots():
    branch_id = request.args.get("branch_id") or ""
    subject_id = request.args.get("subject_id") or ""
    selected_date = request.args.get("date") or date.today().isoformat()
    section = (request.args.get("section") or "").strip()
    period = request.args.get("period") or ""
    time_override = (request.args.get("time") or "").strip()
    db = None
    try:
        db = get_db()
        context = _resolve_timetable_slots(
            db,
            branch_id,
            subject_id,
            selected_date,
            section=section,
            period=period,
            time_override=time_override,
        )
        reason = ""
        if not context.get("slots"):
            try:
                selected_date_obj = date.fromisoformat(selected_date)
            except Exception:
                selected_date_obj = date.today()
            weekday = selected_date_obj.strftime("%A")
            reason = _attendance_no_schedule_reason(db, branch_id, section=section, weekday=weekday)
        context["reason"] = reason
        print(
            f"[api_timetable_slots] branch={branch_id} section={section!r} subject={subject_id!r} rows={len(context.get('slots', []))} reason={reason}"
        )
        return jsonify(context)
    except Exception as e:
        return jsonify({"slots": [], "selected_slot": None, "active_slot": None, "has_schedule": False, "is_today": False, "current_time": "", "weekday": "", "unique_slot": False, "error": str(e)})
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@app.route("/api/attendance-periods")
@safe_api
@login_required
def api_attendance_periods():
    branch_id = request.args.get("branch_id") or ""
    subject_id = request.args.get("subject_id") or ""
    selected_date = request.args.get("date") or date.today().isoformat()
    section = (request.args.get("section") or "").strip()
    period = request.args.get("period") or ""
    timetable_entry_id = request.args.get("timetable_entry_id") or ""
    time_override = (request.args.get("time") or "").strip()
    db = get_db()
    try:
        try:
            context = _resolve_timetable_slots(
                db,
                branch_id,
                subject_id,
                selected_date,
                section=section,
                period=period,
                time_override=time_override,
                timetable_entry_id=timetable_entry_id,
            )
        except Exception as exc:
            print(f"[attendance] api_attendance_periods resolve failed: {repr(exc)}")
            context = {"slots": [], "selected_slot": None, "active_slot": None, "has_schedule": False, "is_today": False, "current_time": "", "weekday": "", "unique_slot": False}
        return jsonify(context)
    finally:
        try:
            db.close()
        except Exception:
            pass


@app.route("/api/timetable-periods")
@safe_api
@login_required
def api_timetable_periods():
    branch_id = request.args.get("branch_id") or ""
    subject_id = request.args.get("subject_id") or ""
    selected_date = request.args.get("date") or date.today().isoformat()
    section = (request.args.get("section") or "").strip()
    time_override = (request.args.get("time") or "").strip()

    db = None
    try:
        db = get_db()
        ctx = _resolve_timetable_slots(db, branch_id, subject_id, selected_date, section=section, time_override=time_override)
        periods = [
            {
                "period": s.get("period"),
                "day": s.get("day"),
                "start_time": s.get("start_time"),
                "end_time": s.get("end_time"),
                "timetable_entry_id": s.get("timetable_entry_id"),
                "subject_name": s.get("subject_name"),
                "subject_id": s.get("subject_id"),
                "room": s.get("room"),
                "faculty": s.get("faculty"),
                "is_active": s.get("is_active"),
            }
            for s in ctx.get("slots", [])
        ]
        reason = ""
        if not periods:
            try:
                selected_date_obj = date.fromisoformat(selected_date)
            except Exception:
                selected_date_obj = date.today()
            weekday = selected_date_obj.strftime("%A")
            reason = _attendance_no_schedule_reason(db, branch_id, section=section, weekday=weekday)
        print(
            f"[api_timetable_periods] branch={branch_id} section={section} subject={subject_id} periods={len(periods)} reason={reason}"
        )
        return jsonify({"periods": periods, "selected": ctx.get("selected_slot"), "active": ctx.get("active_slot"), "unique": ctx.get("unique_slot"), "has_schedule": ctx.get("has_schedule"), "reason": reason})
    except Exception as e:
        print(f"[api_timetable_periods] ERROR: {repr(e)}")
        return jsonify({"periods": [], "selected": None, "active": None, "unique": False, "has_schedule": False, "error": str(e)})
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


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
                counts[t] = int(_safe_fetchone_value(db.execute(f"SELECT COUNT(*) FROM {t}").fetchone(), default=0))
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

        # Diagnostic test for Postgres URL
        if db_str.startswith("postgres"):
            # Print database info safely
            try:
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(db_str)
                host = parsed.hostname
                db_name = parsed.path.lstrip('/')
                username = parsed.username
                port = parsed.port or 5432
                query_params = parse_qs(parsed.query)
                sslmode = query_params.get('sslmode', [None])[0]

                print("[DB DIAGNOSTIC] Safely extracted connection parameters:")
                print(f"  Host: {host}")
                print(f"  Database Name: {db_name}")
                print(f"  Username: {username}")
                print(f"  SSL Mode: {sslmode}")
                print(f"  Port: {port}")

                # Verify whether the application is using internal/external URL
                is_external = False
                if host and ("-a.render.com" in host or "-postgres.render.com" in host) and "-a" in host.split('.')[0]:
                    is_external = True

                if is_external:
                    print("[DB DIAGNOSTIC] Application is using the External Database URL.")
                else:
                    print("[DB DIAGNOSTIC] Application is using the Internal Database URL.")

                # Check host resolution
                import socket
                try:
                    ip = socket.gethostbyname(host)
                    print(f"[DB DIAGNOSTIC] Host {host} resolved to {ip}")
                except Exception as ex:
                    print(f"[DB DIAGNOSTIC ERROR] Host {host} resolution failed: {repr(ex)}")

                # Check port reachability
                try:
                    with socket.create_connection((host, port), timeout=5) as sock:
                        print(f"[DB DIAGNOSTIC] Port {port} is reachable on {host}")
                except Exception as ex:
                    print(f"[DB DIAGNOSTIC ERROR] Port {port} on {host} is unreachable: {repr(ex)}")
                    print("[DB DIAGNOSTIC INFO] Check whether the Render PostgreSQL instance is suspended, deleted, rotated, or has changed credentials.")
            except Exception as ex:
                print(f"[DB DIAGNOSTIC ERROR] Diagnostic gathering failed: {repr(ex)}")

            # Startup test exactly as requested
            try:
                import psycopg2
                url = os.environ.get("DATABASE_URL")
                print("[DB TEST] attempting connection")
                conn = psycopg2.connect(url)
                cur = conn.cursor()
                cur.execute("SELECT version();")
                print(cur.fetchone())
                conn.close()
                print("[DB TEST] connection test passed successfully")
            except Exception as ex:
                print("[DB TEST ERROR] Connection test failed.")
                import traceback
                traceback.print_exc()

        if not db_str.startswith('postgres'):
            print(f"Database file exists: {os.path.exists(db_str)}")
            if os.path.exists(db_str):
                db_size = os.path.getsize(db_str)
                print(f"Database file size: {db_size} bytes")

        # Best-effort schema initialization at startup (won't crash the app).
        init_db()
        print("Database initialized successfully")

        db = None
        try:
            db = get_db()
            _validate_dashboard_schema(db)
            try:
                _ensure_teacher_schema(db)
            except Exception:
                print("[init] ensure teacher schema failed:\n" + traceback.format_exc())
        finally:
            if db:
                try:
                    db.close()
                except Exception:
                    pass
    except Exception as e:
        print(f"Database initialization failed: {repr(e)}")
        print(traceback.format_exc())

# Register timetable routes at import time so they are available under WSGI
# servers (e.g., Gunicorn/Render), not only when running __main__.
try:
    from timetable import register_routes as _register_timetable_routes
    _register_timetable_routes(app, get_db)
    print("Timetable routes registered")
except Exception as e:
    print(f"[INFO] timetable routes registration skipped: {repr(e)}")

# Final safety net: ensure `timetable_home` always exists so templates using
# url_for('timetable_home') never crash with BuildError in production.
if "timetable_home" not in app.view_functions:
    @app.route("/timetable")
    def timetable_home():
        # Surface the original import error in the fallback so logs contain root cause.
        msg = "Timetable module is unavailable right now."
        if timetable_import_error is not None:
            try:
                msg = f"Timetable import failed: {timetable_import_error!r}"
            except Exception:
                msg = "Timetable import failed (see logs for details)."
        err = RuntimeError(msg)
        print("TIMETABLE FALLBACK RAISED:", repr(err))
        traceback.print_exc()
        raise err


# Compatibility endpoints for templates that expect teacher/admin management
# routes from the fuller app variant. These are only added when missing so
# they won't override existing blueprint or route implementations.
if "manage_teachers" not in app.view_functions:
    @app.route("/admin/teachers", methods=["GET", "POST"])
    @login_required
    def manage_teachers():
        if session.get("role") != "admin":
            abort(403)

        db = get_db()
        placeholder = get_placeholder()
        try:
            if request.method == "POST":
                action = (request.form.get("action") or "").strip()
                teacher_id = (request.form.get("teacher_id") or "").strip()
                if action == "add":
                    username = (request.form.get("username") or "").strip()
                    password = (request.form.get("password") or "").strip()
                    if username and password:
                        db.execute(
                            f"INSERT INTO users (username, password, role) VALUES ({placeholder}, {placeholder}, {placeholder})",
                            (username, generate_password_hash(password), "teacher"),
                        )
                        db.commit()
                        flash("Teacher account created.", "success")
                    else:
                        flash("Username and password are required.", "error")
                elif action == "delete" and teacher_id.isdigit():
                    db.execute(f"DELETE FROM users WHERE id = {placeholder} AND role = {placeholder}", (int(teacher_id), "teacher"))
                    db.commit()
                    flash("Teacher deleted.", "success")
                elif action == "reset_password" and teacher_id.isdigit():
                    new_password = (request.form.get("new_password") or "").strip()
                    if len(new_password) < 4:
                        flash("Password must be at least 4 characters.", "error")
                    else:
                        db.execute(
                            f"UPDATE users SET password = {placeholder} WHERE id = {placeholder} AND role = {placeholder}",
                            (generate_password_hash(new_password), int(teacher_id), "teacher"),
                        )
                        db.commit()
                        flash("Teacher password reset.", "success")
                elif action == "edit" and teacher_id.isdigit():
                    username = (request.form.get("username") or "").strip()
                    if username:
                        db.execute(
                            f"UPDATE users SET username = {placeholder} WHERE id = {placeholder} AND role = {placeholder}",
                            (username, int(teacher_id), "teacher"),
                        )
                        db.commit()
                        flash("Teacher updated.", "success")

            teachers = db.execute(
                f"SELECT id, username, NULL AS name, NULL AS subject_name FROM users WHERE role = {placeholder} ORDER BY id DESC",
                ("teacher",),
            ).fetchall()
            subjects = db.execute("SELECT id, name FROM subjects ORDER BY name").fetchall()
            branches = db.execute("SELECT id, name FROM branches ORDER BY name").fetchall()

            teacher_branches_map = {row_get(t, "id"): [] for t in teachers}
            teacher_subjects_map = {row_get(t, "id"): [] for t in teachers}

            return render_template(
                "admin_teachers.html",
                teachers=teachers,
                subjects=subjects,
                branches=branches,
                teacher_branches_map=teacher_branches_map,
                teacher_subjects_map=teacher_subjects_map,
            )
        finally:
            try:
                db.close()
            except Exception:
                pass


if "admin_academic" not in app.view_functions:
    @app.route("/admin/academic", methods=["GET", "POST"])
    @login_required
    def admin_academic():
        if session.get("role") != "admin":
            abort(403)

        db = get_db()
        try:
            students = []
            stats = []
            try:
                students = db.execute(
                    "SELECT id, name, enrollment, current_year, current_semester FROM students ORDER BY name"
                ).fetchall()
                stats = db.execute(
                    "SELECT current_year, current_semester, COUNT(*) AS count FROM students GROUP BY current_year, current_semester ORDER BY current_year, current_semester"
                ).fetchall()
            except Exception:
                # Older schema may not have academic columns.
                students = []
                stats = []
            return render_template("admin_academic.html", students=students, stats=stats)
        finally:
            try:
                db.close()
            except Exception:
                pass


if "delete_subject" not in app.view_functions:
    @app.route("/delete_subject", methods=["POST"])
    @login_required
    def delete_subject():
        if session.get("role") != "admin":
            abort(403)
        subject_id = request.form.get("subject_id") or request.form.get("id")
        if not (subject_id and str(subject_id).isdigit()):
            flash("Invalid subject id.", "error")
            return redirect(url_for("subjects"))
        db = get_db()
        placeholder = get_placeholder()
        try:
            db.execute(f"DELETE FROM subjects WHERE id = {placeholder}", (int(subject_id),))
            db.commit()
            flash("Subject deleted.", "success")
        except Exception as e:
            db.rollback()
            flash(f"Unable to delete subject: {repr(e)}", "error")
        finally:
            try:
                db.close()
            except Exception:
                pass
        return redirect(url_for("subjects"))


if "delete_student" not in app.view_functions:
    @app.route("/delete_student", methods=["POST"])
    @login_required
    def delete_student():
        if session.get("role") != "admin":
            abort(403)
        student_id = request.form.get("student_id") or request.form.get("id")
        if not (student_id and str(student_id).isdigit()):
            flash("Invalid student id.", "error")
            return redirect(url_for("students"))
        db = get_db()
        placeholder = get_placeholder()
        try:
            db.execute(f"DELETE FROM attendance WHERE student_id = {placeholder}", (int(student_id),))
            db.execute(f"DELETE FROM users WHERE student_id = {placeholder}", (int(student_id),))
            db.execute(f"DELETE FROM students WHERE id = {placeholder}", (int(student_id),))
            db.commit()
            flash("Student deleted.", "success")
        except Exception as e:
            db.rollback()
            flash(f"Unable to delete student: {repr(e)}", "error")
        finally:
            try:
                db.close()
            except Exception:
                pass
        return redirect(url_for("students"))


if "bulk_delete_students" not in app.view_functions:
    @app.route("/bulk_delete_students", methods=["POST"])
    @login_required
    def bulk_delete_students():
        if session.get("role") != "admin":
            abort(403)
        ids = request.form.getlist("student_ids")
        valid_ids = [int(x) for x in ids if str(x).isdigit()]
        if not valid_ids:
            flash("No students selected.", "warning")
            return redirect(url_for("students"))
        db = get_db()
        placeholder = get_placeholder()
        try:
            marks = ",".join([placeholder] * len(valid_ids))
            db.execute(f"DELETE FROM attendance WHERE student_id IN ({marks})", tuple(valid_ids))
            db.execute(f"DELETE FROM users WHERE student_id IN ({marks})", tuple(valid_ids))
            db.execute(f"DELETE FROM students WHERE id IN ({marks})", tuple(valid_ids))
            db.commit()
            flash(f"Deleted {len(valid_ids)} students.", "success")
        except Exception as e:
            db.rollback()
            flash(f"Bulk delete failed: {repr(e)}", "error")
        finally:
            try:
                db.close()
            except Exception:
                pass
        return redirect(url_for("students"))


if "delete_all_students" not in app.view_functions:
    @app.route("/delete_all_students", methods=["POST"])
    @login_required
    def delete_all_students():
        if session.get("role") != "admin":
            abort(403)
        db = get_db()
        try:
            db.execute("DELETE FROM attendance")
            db.execute("DELETE FROM users WHERE role = 'student'")
            db.execute("DELETE FROM students")
            db.commit()
            flash("All students deleted.", "success")
        except Exception as e:
            db.rollback()
            flash(f"Delete all failed: {repr(e)}", "error")
        finally:
            try:
                db.close()
            except Exception:
                pass
        return redirect(url_for("students"))


if "teacher_dashboard" not in app.view_functions:
    @app.route("/teacher/dashboard")
    @login_required
    def teacher_dashboard():
        if session.get("role") != "teacher":
            flash("Teacher access required.", "warning")
            return redirect(url_for("dashboard"))
        flash("Teacher dashboard is not fully enabled in this deployment.", "warning")
        return redirect(url_for("mark_attendance"))


if "teacher_select_branch" not in app.view_functions:
    @app.route("/teacher/select-branch", methods=["GET", "POST"])
    @login_required
    def teacher_select_branch():
        return redirect(url_for("teacher_dashboard"))


if "teacher_select_subject" not in app.view_functions:
    @app.route("/teacher/select-subject", methods=["GET", "POST"])
    @login_required
    def teacher_select_subject():
        return redirect(url_for("teacher_dashboard"))


if "teacher_mark_attendance" not in app.view_functions:
    @app.route("/teacher/attendance", methods=["GET", "POST"])
    @login_required
    def teacher_mark_attendance():
        return redirect(url_for("mark_attendance"))


if "teacher_attendance_records" not in app.view_functions:
    @app.route("/teacher/records")
    @login_required
    def teacher_attendance_records():
        return redirect(url_for("attendance_report"))


if "timetable_manage" not in app.view_functions:
    @app.route("/timetable/manage")
    @login_required
    def timetable_manage():
        return redirect(url_for("timetable_home"))


if "timetable_faculty_schedules" not in app.view_functions:
    @app.route("/timetable/faculty-schedules")
    @login_required
    def timetable_faculty_schedules():
        return redirect(url_for("timetable_home"))


if "timetable_admin_bulk_resolve" not in app.view_functions:
    @app.route("/timetable/admin/bulk-resolve", methods=["POST"])
    @login_required
    def timetable_admin_bulk_resolve():
        flash("Bulk resolve is unavailable in this deployment.", "warning")
        return redirect(url_for("timetable_home"))

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


@app.route("/api/attendance/session", methods=["POST"])
def api_create_attendance_session():
    data = request.get_json() or {}
    section = (data.get("section") or "").strip()
    subject = (data.get("subject") or "").strip()
    faculty = (data.get("faculty") or "").strip()
    date_val = (data.get("date") or "").strip()
    start_time = (data.get("start_time") or "").strip()
    end_time = (data.get("end_time") or "").strip()

    if not all([section, subject, date_val, start_time, end_time]):
        return jsonify({"success": False, "error": "Missing required fields"}), 400

    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        
        # Auto-link to timetable_entries
        # We find the entry that matches section, subject, date (mapped to day), and times
        day_of_week = datetime.strptime(date_val, "%Y-%m-%d").strftime("%A")
        
        te_query = f"""
            SELECT te.id FROM timetable_entries te
            LEFT JOIN subjects s ON te.subject_id = s.id
            WHERE LOWER(TRIM(te.section)) = LOWER(TRIM({placeholder}))
              AND LOWER(TRIM(s.name)) = LOWER(TRIM({placeholder}))
              AND LOWER(TRIM(te.day)) = LOWER(TRIM({placeholder}))
              AND te.start_time = {placeholder}
              AND te.end_time = {placeholder}
            LIMIT 1
        """
        te_row = db.execute(te_query, (section, subject, day_of_week, start_time, end_time)).fetchone()
        timetable_entry_id = row_get(te_row, "id") if te_row else None
        
        insert_query = f"""
            INSERT INTO attendance_sessions 
            (timetable_entry_id, faculty_name, section, subject_name, date, start_time, end_time)
            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
        """
        if app.config.get("DATABASE", "").startswith("postgres"):
            insert_query += " RETURNING id"
        
        try:
            cur = db.execute(insert_query, (timetable_entry_id, faculty, section, subject, date_val, start_time, end_time))
            if app.config.get("DATABASE", "").startswith("postgres"):
                session_id = _safe_fetchone_value(cur.fetchone(), default=0)
            else:
                session_id = cur.lastrowid
            db.commit()
            return jsonify({"success": True, "session_id": session_id})
        except Exception as e:
            db.rollback()
            err_str = str(e).lower()
            if "unique" in err_str or "duplicate" in err_str:
                return jsonify({"success": False, "error": "Session already exists for this slot."}), 409
            raise e
            
    except Exception as e:
        print(f"Error creating attendance session: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@app.route("/api/attendance/mark", methods=["POST"])
def api_mark_attendance():
    data = request.get_json() or {}
    session_id = data.get("session_id")
    student_id = data.get("student_id")
    status = (data.get("status") or "").strip()

    if not all([session_id, student_id, status]):
        return jsonify({"success": False, "error": "Missing required fields"}), 400

    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        
        # Check if session is closed
        sess = db.execute(f"SELECT is_closed FROM attendance_sessions WHERE id = {placeholder}", (session_id,)).fetchone()
        if not sess:
            return jsonify({"success": False, "error": "Session not found."}), 404
        if row_get(sess, "is_closed"):
            return jsonify({"success": False, "error": "Session is closed."}), 403

        insert_query = f"""
            INSERT INTO attendance_records (session_id, student_id, status)
            VALUES ({placeholder}, {placeholder}, {placeholder})
        """
        # Handle SQLite/Postgres "ON CONFLICT" or simply let it fail if unique constraint violated
        # Using a simple check first to prevent error spam
        existing = db.execute(f"SELECT id FROM attendance_records WHERE session_id = {placeholder} AND student_id = {placeholder}", (session_id, student_id)).fetchone()
        
        if existing:
            db.execute(f"UPDATE attendance_records SET status = {placeholder} WHERE session_id = {placeholder} AND student_id = {placeholder}", (status, session_id, student_id))
        else:
            db.execute(insert_query, (session_id, student_id, status))
            
        db.commit()
        return jsonify({"success": True})
    except Exception as e:
        print(f"Error marking attendance: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@app.route("/api/attendance/session/<int:session_id>/close", methods=["POST"])
def api_close_attendance_session(session_id):
    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        
        db.execute(f"UPDATE attendance_sessions SET is_closed = 1 WHERE id = {placeholder}", (session_id,))
        db.commit()
        return jsonify({"success": True})
    except Exception as e:
        print(f"Error closing attendance session: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@app.route("/api/attendance/session/<int:session_id>/bulk-mark", methods=["POST"])
def api_bulk_mark_attendance(session_id):
    data = request.get_json() or {}
    records = data.get("records")
    
    if not isinstance(records, list):
        return jsonify({"success": False, "error": "Invalid payload format."}), 400

    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        
        sess = db.execute(f"SELECT is_closed FROM attendance_sessions WHERE id = {placeholder}", (session_id,)).fetchone()
        if not sess or row_get(sess, "is_closed"):
            return jsonify({"success": False, "error": "Session closed or not found."}), 403

        for rec in records:
            student_id = rec.get("student_id")
            status = rec.get("status")
            if not student_id or not status: continue
            
            existing = db.execute(f"SELECT id FROM attendance_records WHERE session_id = {placeholder} AND student_id = {placeholder}", (session_id, student_id)).fetchone()
            if existing:
                db.execute(f"UPDATE attendance_records SET status = {placeholder} WHERE session_id = {placeholder} AND student_id = {placeholder}", (status, session_id, student_id))
            else:
                db.execute(f"INSERT INTO attendance_records (session_id, student_id, status) VALUES ({placeholder}, {placeholder}, {placeholder})", (session_id, student_id, status))
                
        db.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if db:
            try: db.close()
            except: pass


@app.route("/attendance/session/<int:session_id>/mark", methods=["GET"])
@login_required
def session_mark_attendance_ui(session_id):
    db = None
    try:
        db = get_db()
        placeholder = get_placeholder()
        
        session_row = db.execute(f"SELECT * FROM attendance_sessions WHERE id = {placeholder}", (session_id,)).fetchone()
        if not session_row:
            flash("Session not found.", "error")
            return redirect(url_for("dashboard"))
            
        session_section = row_get(session_row, "section")
        
        # Fetch students for this section
        students = db.execute(f"SELECT id, name, roll_no, enrollment, section FROM students WHERE LOWER(TRIM(section)) = LOWER(TRIM({placeholder})) ORDER BY roll_no, name", (session_section,)).fetchall()
        
        # Fetch existing records to pre-populate UI
        records = db.execute(f"SELECT student_id, status FROM attendance_records WHERE session_id = {placeholder}", (session_id,)).fetchall()
        record_map = {row_get(r, "student_id"): row_get(r, "status") for r in records}
        
        return render_template("session_mark_attendance.html", session=session_row, students=students, record_map=record_map)
    except Exception as e:
        print(f"Error loading marking UI: {e}")
        flash("Internal error loading marking UI.", "error")
        return redirect(url_for("dashboard"))
    finally:
        if db:
            try: db.close()
            except: pass


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
