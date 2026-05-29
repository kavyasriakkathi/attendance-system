import os
import re
from datetime import date, timedelta, datetime
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


def _coerce_int(v):
    try:
        if v is None or v == "":
            return None
        return int(v)
    except Exception:
        return None


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
    # Split by hyphen, slash, or space
    parts = re.split(r"[-/ ]+", v)
    if len(parts) >= 2:
        return parts[0], parts[-1]
    # Check if name is like CSEA or CSMB
    m = re.match(r"^([A-Z]+)([A-Z0-9])$", v)
    if m:
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


def _get_timetable_subjects_for_branch(db, branch_id, section=None):
    placeholder = get_placeholder()

    # Resolve numeric id or branch name token
    selected_branch_id = None
    selected_branch_name = ""
    branch_id_val = _coerce_int(branch_id)
    if branch_id_val is not None:
        r = db.execute(f"SELECT id, name FROM branches WHERE id = {placeholder}", (branch_id_val,)).fetchone()
        if r:
            selected_branch_id = row_get(r, "id")
            selected_branch_name = row_get(r, "name") or ""
    if not selected_branch_name and branch_id:
        r = db.execute(f"SELECT id, name FROM branches WHERE UPPER(name) = {placeholder}", (str(branch_id).strip().upper(),)).fetchone()
        if r:
            selected_branch_id = row_get(r, "id")
            selected_branch_name = row_get(r, "name") or str(branch_id)
        else:
            selected_branch_name = str(branch_id)

    base_branch_name, derived_section = split_branch_section(selected_branch_name)
    # Prefer explicitly provided section parameter over derived section
    section_val = (section or derived_section or "").strip()

    # Collect branch ids matching the base token (prefix match), so selecting
    # 'CSE' includes 'CSE-A','CSE-B', etc.
    branch_ids_to_match = []
    try:
        if base_branch_name:
            rows = db.execute("SELECT id FROM branches WHERE UPPER(name) LIKE ?", (base_branch_name.upper() + '%',)).fetchall()
            branch_ids_to_match = [row_get(r, 'id') for r in rows if row_get(r, 'id')]
    except Exception:
        branch_ids_to_match = []

    if not branch_ids_to_match and selected_branch_id:
        branch_ids_to_match = [selected_branch_id]

    # Query timetable entries for the collected branch ids
    subjects = []
    seen = set()
    try:
        if branch_ids_to_match:
            ph = ",".join([placeholder] * len(branch_ids_to_match))
            sql = (
                "SELECT te.*, s.name AS subject_name_db, b.name AS branch_name_db "
                "FROM timetable_entries te "
                "LEFT JOIN subjects s ON te.subject_id = s.id "
                "LEFT JOIN branches b ON te.branch_id = b.id "
                f"WHERE te.branch_id IN ({ph})"
            )
            rows = db.execute(sql, tuple(branch_ids_to_match)).fetchall()
        else:
            sql = (
                "SELECT te.*, s.name AS subject_name_db, b.name AS branch_name_db "
                "FROM timetable_entries te "
                "LEFT JOIN subjects s ON te.subject_id = s.id "
                "LEFT JOIN branches b ON te.branch_id = b.id "
            )
            rows = db.execute(sql).fetchall()
    except Exception:
        rows = []

    for r in rows:
        if section_val:
            if not section_matches(row_get(r, 'section'), section_val):
                continue
        s_name = (row_get(r, 'subject_name_db') or row_get(r, 'subject_name') or '').strip()
        if not s_name:
            continue
        display = get_subject_display_name(s_name)
        key = normalize_text(display)
        if key in seen:
            continue
        seen.add(key)
        s_id = row_get(r, 'subject_id')
        subjects.append({'id': s_id, 'name': display, 'canonical': s_name})

    subjects.sort(key=lambda x: x['name'])
    return subjects


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
        "database_info": {
            "storage": "PostgreSQL" if str(app.config.get("DATABASE", "")).startswith("postgres") else "SQLite",
            "path": app.config.get("DATABASE", ""),
        },
        "mail_info": {
            "configured": is_mail_configured(),
            "server": app.config.get("MAIL_SERVER"),
            "port": app.config.get("MAIL_PORT"),
            "username": app.config.get("MAIL_USERNAME"),
            "tls": app.config.get("MAIL_USE_TLS"),
            "render_env": render_env,
        },
        "persistence_warning": render_env and not str(app.config.get("DATABASE", "")).startswith("postgres"),
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
        "attendance": {"id", "student_id", "branch_id", "subject_id", "date", "status"},
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


def teacher_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("role") != "teacher" or not session.get("teacher_id"):
            flash("Please log in as a teacher to continue.", "warning")
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
                    f"SELECT id, username, username AS name, password, subject_id, branch_id, subject_name FROM users WHERE id = {placeholder} AND role = {placeholder}",
                    (teacher_id, "teacher"),
                ).fetchone()
            except Exception:
                teacher = None
        if not teacher:
            return None

        assigned_branches, assigned_subjects = _resolve_teacher_assignments(db, teacher_id)
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
        CREATE TABLE IF NOT EXISTS subject_aliases (
            id SERIAL PRIMARY KEY,
            alias TEXT UNIQUE NOT NULL,
            canonical_name TEXT NOT NULL
        );
        """)

        db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_student_subject_date
        ON attendance(student_id, subject_id, date);
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

        CREATE TABLE IF NOT EXISTS subject_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT UNIQUE NOT NULL,
            canonical_name TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_student_subject_date
        ON attendance(student_id, subject_id, date);

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
            import timetable as _timetable
            current_active_period = _timetable.get_global_active_class(db)
            upcoming_timetable = _timetable.get_upcoming_classes(db, "", "", limit=4)
        except Exception as timetable_err:
            print(f"[dashboard] timetable lookup failed: {repr(timetable_err)}")

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
def upload_students_csv():
    """Simple CSV upload for students with automatic login creation."""
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename.endswith(".csv"):
            flash("Please upload a valid CSV file.", "error")
            return redirect(url_for("upload_students_csv"))

        try:
            import pandas as pd
            # Use pandas to read CSV for better handling of delimiters and whitespace
            df = pd.read_csv(file)
            
            # Standardize column names to lowercase
            df.columns = [str(c).strip().lower() for c in df.columns]
            
            required = {"name", "enrollment", "email", "branch_id"}
            if not required.issubset(set(df.columns)):
                flash(f"CSV must contain: {', '.join(required)}", "error")
                return redirect(url_for("upload_students_csv"))

            db = get_db()
            placeholder = get_placeholder()
            is_postgres = str(app.config.get("DATABASE", "")).startswith("postgres")
            
            inserted = 0
            skipped = 0

            for _, row in df.iterrows():
                name = str(row["name"]).strip()
                enrollment = str(row["enrollment"]).strip()
                email = str(row["email"]).strip()
                branch_id = str(row["branch_id"]).strip()

                if not name or not enrollment or not branch_id:
                    continue

                # 1) Duplicate Check
                existing = db.execute(f"SELECT id FROM students WHERE enrollment = {placeholder}", (enrollment,)).fetchone()
                if existing:
                    skipped += 1
                    continue

                try:
                    # 2) Insert Student
                    if is_postgres:
                        cur = db.execute(
                            f"INSERT INTO students (name, enrollment, email, branch_id) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}) RETURNING id",
                            (name, enrollment, email or None, branch_id)
                        )
                        student_id = _safe_fetchone_value(cur.fetchone(), default=0)
                    else:
                        cur = db.execute(
                            f"INSERT INTO students (name, enrollment, email, branch_id) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})", (name, enrollment, email or None, branch_id)
                        )
                        student_id = cur.lastrowid

                    # 3) Create User Account (Password = last 4 digits of enrollment)
                    password_plain = enrollment[-4:] if len(enrollment) >= 4 else enrollment
                    password_hash = generate_password_hash(password_plain)
                    
                    db.execute(
                        f"INSERT INTO users (username, password, role, student_id) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})",
                        (enrollment, password_hash, "student", student_id)
                    )
                    inserted += 1
                except Exception as e:
                    print(f"[CSV Upload Error] Row {enrollment}: {repr(e)}")
                    continue

            db.commit()
            db.close()
            flash(f"Upload complete! {inserted} students added, {skipped} skipped.", "success")
            return redirect(url_for("students"))

        except Exception as e:
            print(f"[CSV CRITICAL] {repr(e)}")
            flash("Failed to process CSV file. Ensure it is correctly formatted.", "error")
            return redirect(url_for("upload_students_csv"))

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
        query += " ORDER BY students.name"
        
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


def _resolve_timetable_slots(db, branch_id="", subject_id="", selected_date=None, section="", period="", time_override=""):
    selected_date_obj = selected_date or date.today()
    if isinstance(selected_date_obj, str):
        try:
            selected_date_obj = date.fromisoformat(selected_date_obj)
        except Exception:
            selected_date_obj = date.today()

    current_time = _attendance_pick_time(time_override)
    weekday = selected_date_obj.strftime("%A")
    weekday_short = selected_date_obj.strftime("%a")
    is_today = selected_date_obj == date.today()

    # 1. Resolve selected branch details
    selected_branch_id = None
    selected_branch_name = ""
    placeholder = get_placeholder()
    
    branch_id_val = _coerce_int(branch_id)
    if branch_id_val is not None:
        row = db.execute(f"SELECT id, name FROM branches WHERE id = {placeholder}", (branch_id_val,)).fetchone()
        if row:
            selected_branch_id = row_get(row, "id")
            selected_branch_name = row_get(row, "name") or ""
    if not selected_branch_name and branch_id:
        row = db.execute(f"SELECT id, name FROM branches WHERE UPPER(name) = {placeholder}", (str(branch_id).strip().upper(),)).fetchone()
        if row:
            selected_branch_id = row_get(row, "id")
            selected_branch_name = row_get(row, "name") or ""
            
    # 2. Derive base branch and section
    base_branch_name, derived_section = split_branch_section(selected_branch_name or branch_id)
    # If no section is explicitly provided, use the derived section from the branch
    section_val = (section or "").strip()
    if not section_val and derived_section:
        section_val = derived_section

    base_branch_id = None
    if base_branch_name:
        row = db.execute(f"SELECT id FROM branches WHERE UPPER(name) = {placeholder}", (base_branch_name.upper(),)).fetchone()
        if row:
            base_branch_id = row_get(row, "id")

    # 3. Resolve selected subject details
    req_subject_id = _coerce_int(subject_id)
    req_subject_name = ""
    if req_subject_id is not None:
        row = db.execute(f"SELECT id, name FROM subjects WHERE id = {placeholder}", (req_subject_id,)).fetchone()
        if row:
            req_subject_name = row_get(row, "name") or ""
    if not req_subject_name and subject_id:
        req_subject_name = str(subject_id).strip()

    # Debug Logs (Point 5)
    print("--- TIMETABLE LOOKUP DEBUG LOGS ---")
    print(f"Selected branch input: {branch_id} (Resolved ID: {selected_branch_id}, Name: {selected_branch_name})")
    print(f"Derived base branch name: {base_branch_name} (ID: {base_branch_id}), Derived section: {section_val}")
    print(f"Selected subject input: {subject_id} (Resolved Name: {req_subject_name})")
    print(f"Selected date: {selected_date} (Weekday: {weekday})")

    # 4. Fetch candidate entries
    entries_sql = (
        "SELECT te.*, s.name AS subject_name_db, t.name AS teacher_name_db, b.name AS branch_name_db "
        "FROM timetable_entries te "
        "LEFT JOIN subjects s ON te.subject_id = s.id "
        "LEFT JOIN teachers t ON te.teacher_id = t.id "
        "LEFT JOIN branches b ON te.branch_id = b.id "
    )
    
    try:
        raw_entries = db.execute(entries_sql).fetchall()
    except Exception as e:
        print(f"Error fetching timetable_entries: {e}")
        raw_entries = []

    try:
        raw_slots = db.execute("SELECT ts.*, ts.branch AS branch_name_db FROM timetable_slots ts").fetchall()
    except Exception as e:
        raw_slots = []

    candidate_rows = []
    for r in raw_entries:
        candidate_rows.append({
            "id": row_get(r, "id"),
            "timetable_entry_id": row_get(r, "id"),
            "branch_id": row_get(r, "branch_id"),
            "branch_name": row_get(r, "branch_name_db") or "",
            "section": row_get(r, "section") or "",
            "subject_id": row_get(r, "subject_id"),
            "subject_name": row_get(r, "subject_name_db") or row_get(r, "subject_name") or "",
            "faculty_name": row_get(r, "teacher_name_db") or row_get(r, "faculty_name") or "",
            "room": row_get(r, "room") or "",
            "day": row_get(r, "day") or "",
            "start_time": row_get(r, "start_time") or "",
            "end_time": row_get(r, "end_time") or "",
            "is_lab": row_get(r, "is_lab") or 0,
            "source": "normalized"
        })
        
    for r in raw_slots:
        candidate_rows.append({
            "id": row_get(r, "id"),
            "timetable_entry_id": row_get(r, "id"),
            "branch_id": None,
            "branch_name": row_get(r, "branch_name_db") or row_get(r, "branch") or "",
            "section": row_get(r, "section") or "",
            "subject_id": None,
            "subject_name": row_get(r, "subject_name") or "",
            "faculty_name": row_get(r, "faculty_name") or row_get(r, "teacher_name") or "",
            "room": row_get(r, "room") or "",
            "day": row_get(r, "day") or "",
            "start_time": row_get(r, "start_time") or "",
            "end_time": row_get(r, "end_time") or "",
            "is_lab": row_get(r, "is_lab") or 0,
            "source": "legacy"
        })

    # 5. Apply matching filters
    matched_rows = []
    reasons = []
    
    branch_ids_to_match = [selected_branch_id] if selected_branch_id else []
    if base_branch_id and base_branch_id not in branch_ids_to_match:
        branch_ids_to_match.append(base_branch_id)

    for r in candidate_rows:
        if not (day_matches(r["day"], weekday) or day_matches(r["day"], weekday_short)):
            continue
            
        br_match = False
        r_br_id = r["branch_id"]
        r_br_name = r["branch_name"]
        
        if r_br_id in branch_ids_to_match:
            br_match = True
        elif selected_branch_name or base_branch_name:
            r_br_norm = normalize_text(r_br_name)
            if r_br_norm == normalize_text(selected_branch_name):
                br_match = True
            elif r_br_norm == normalize_text(base_branch_name):
                br_match = True
            else:
                r_br_base, _ = split_branch_section(r_br_name)
                if r_br_base and normalize_text(r_br_base) == normalize_text(base_branch_name):
                    br_match = True

        if not br_match:
            reasons.append(f"Branch mismatch: entry branch '{r_br_name}' (ID {r_br_id}), expected ID {selected_branch_id} or Name '{selected_branch_name}'")
            continue

        if section_val:
            if not section_matches(r["section"], section_val):
                reasons.append(f"Section mismatch: entry section '{r['section']}', expected '{section_val}'")
                continue

        if req_subject_name:
            if not subject_matches(r["subject_id"], r["subject_name"], req_subject_id, req_subject_name):
                reasons.append(f"Subject mismatch: entry subject '{r['subject_name']}', expected '{req_subject_name}'")
                continue

        matched_rows.append(r)

    print(f"Matched timetable entries: {len(matched_rows)}")
    for m in matched_rows:
        print(f" -> MATCHED: Period time: {m['start_time']}-{m['end_time']}, Subject: {m['subject_name']}, Faculty: {m['faculty_name']}, Room: {m['room']}, Section: {m['section']}")
        
    if not matched_rows:
        print("REAL REASONS FOR NO MATCHES:")
        for reason in set(reasons[:15]):
            print(f" - {reason}")

    # 6. Build final slots list and select active/unique/nearest slot
    slots = []
    seen = set()
    active_index = None
    selected_index = None

    matched_rows.sort(key=lambda x: (x["start_time"], x["end_time"]))
    
    for row in matched_rows:
        start_time = row["start_time"]
        end_time = row["end_time"]
        if not start_time or not end_time:
            continue
            
        dedupe_key = (
            start_time,
            end_time,
            normalize_text(row["subject_name"]),
            normalize_text(row["faculty_name"]),
            normalize_text(row["room"]),
            normalize_text(row["section"]),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        
        is_active = bool(is_today and start_time <= current_time <= end_time)
        slot = {
            "period": len(slots) + 1,
            "timetable_entry_id": row["timetable_entry_id"],
            "branch_id": row["branch_id"] or selected_branch_id,
            "branch_name": row["branch_name"] or selected_branch_name,
            "section": row["section"] or section_val,
            "subject_id": row["subject_id"] or req_subject_id,
            "subject_name": row["subject_name"],
            "faculty_name": row["faculty_name"],
            "faculty": row["faculty_name"],
            "room": row["room"],
            "start_time": start_time,
            "end_time": end_time,
            "day": row["day"],
            "is_active": is_active,
            "source": row["source"]
        }
        
        if is_active and active_index is None:
            active_index = len(slots)
        slots.append(slot)

    if period:
        for idx, slot in enumerate(slots):
            if str(slot["period"]) == str(period):
                selected_index = idx
                break
                
    if selected_index is None and len(slots) == 1:
        selected_index = 0
        
    if selected_index is None and active_index is not None:
        selected_index = active_index
        
    if selected_index is None and slots:
        if not is_today:
            selected_index = 0
        else:
            upcoming = None
            past = None
            for idx, slot in enumerate(slots):
                st = slot.get('start_time')
                if st and st >= current_time:
                    upcoming = idx
                    break
                past = idx
            if upcoming is not None:
                selected_index = upcoming
            elif past is not None:
                selected_index = past

    selected_slot = slots[selected_index] if selected_index is not None and selected_index < len(slots) else None
    active_slot = slots[active_index] if active_index is not None and active_index < len(slots) else None

    print(f"Final selected slot index: {selected_index} (Selected: {selected_slot is not None})")
    print("-----------------------------------")

    return {
        "slots": slots,
        "selected_slot": selected_slot,
        "active_slot": active_slot,
        "has_schedule": bool(slots),
        "is_today": is_today,
        "current_time": current_time,
        "weekday": weekday,
        "unique_slot": len(slots) == 1,
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


@app.route("/attendance", methods=["GET", "POST"])
@login_required
def mark_attendance():
    db = get_db()
    placeholder = get_placeholder()
    branches = db.execute("SELECT * FROM branches ORDER BY name").fetchall()
    branch_id = request.values.get("branch_id") or ""
    subject_id = request.values.get("subject_id") or ""
    section = (request.values.get("section") or "").strip()
    selected_date = request.values.get("date") or date.today().isoformat()
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

    current_date_obj = selected_date_obj
    prev_date = (current_date_obj - timedelta(days=1)).isoformat()
    next_date = (current_date_obj + timedelta(days=1)).isoformat()

    # Determine base/derived branch, section and resolved subject
    branch_id_val = _coerce_int(branch_id)
    derived_section = ""
    if branch_id:
        try:
            if branch_id_val is not None:
                row = db.execute(f"SELECT name FROM branches WHERE id = {placeholder}", (branch_id_val,)).fetchone()
                if row:
                    br_name = row_get(row, "name") or ""
                    _, derived_sec = split_branch_section(br_name)
                    if derived_sec:
                        derived_section = derived_sec
            else:
                _, derived_sec = split_branch_section(branch_id)
                if derived_sec:
                    derived_section = derived_sec
        except Exception:
            pass

    if not section and derived_section:
        section = derived_section

    # Load subjects for the selected branch/section based on timetable entries
    if branch_id:
        try:
            subjects = _get_timetable_subjects_for_branch(db, branch_id)
        except Exception as e:
            print(f"[ERROR] Subject loading failed: {repr(e)}")
            subjects = []

    # Resolve subject_id to integer if it is an alias/string
    resolved_subject_id = subject_id
    subject_id_val = _coerce_int(subject_id)
    if subject_id_val is not None:
        resolved_subject_id = subject_id_val
    elif subject_id and branch_id:
        for s in subjects:
            if s["id"] and subject_name_matches(s["name"], subject_id):
                resolved_subject_id = s["id"]
                break
    resolved_subject_id = _coerce_int(resolved_subject_id) or resolved_subject_id

    timetable_context = {"slots": [], "selected_slot": None, "active_slot": None, "has_schedule": False, "is_today": False, "current_time": "", "weekday": "", "unique_slot": False}
    if branch_id and subject_id:
        try:
            timetable_context = _resolve_timetable_slots(db, branch_id, subject_id, selected_date, section=section, period=period)
            print(f"[mark_attendance] Resolved timetable_context slots={len(timetable_context.get('slots', []))} selected_slot={bool(timetable_context.get('selected_slot'))} active_slot={bool(timetable_context.get('active_slot'))} unique={timetable_context.get('unique_slot')}")
        except Exception as exc:
            print(f"[attendance] timetable resolve failed: {repr(exc)}")
            timetable_context = {"slots": [], "selected_slot": None, "active_slot": None, "has_schedule": False, "is_today": False, "current_time": "", "weekday": "", "unique_slot": False}
        if not period and timetable_context["selected_slot"]:
            period = str(timetable_context["selected_slot"].get("period") or "")
        elif period and timetable_context["selected_slot"]:
            valid_periods = {str(item.get("period")) for item in timetable_context["slots"]}
            if period not in valid_periods:
                period = str(timetable_context["selected_slot"].get("period") or period)
        if timetable_context["selected_slot"]:
            print(f"[mark_attendance] Selected slot present for branch={branch_id} section={section} subject={subject_id}")
            students = _attendance_students_for_branch(db, branch_id, section)
            student_count = len(students) if isinstance(students, (list, tuple)) else 0
            print(f"[mark_attendance] Loaded students count={student_count}")
            existing_dates = db.execute(
                f"SELECT date, COUNT(*) as count FROM attendance WHERE branch_id = {placeholder} AND subject_id = {placeholder} GROUP BY date ORDER BY date DESC",
                (branch_id, resolved_subject_id),
            ).fetchall()

    if request.method == "POST":
        branch_id = request.form.get("branch_id") or ""
        subject_id = request.form.get("subject_id") or ""
        section = (request.form.get("section") or section or "").strip()
        selected_date = request.form.get("date") or date.today().isoformat()
        period = request.form.get("period") or period or ""
        try:
            selected_date_obj = date.fromisoformat(selected_date)
        except ValueError:
            selected_date_obj = today_date

        if selected_date_obj > today_date:
            selected_date_obj = today_date

        selected_date = selected_date_obj.isoformat()
        
        # Load subjects again for the selected branch/section based on timetable entries
        if branch_id:
            try:
                subjects = _get_timetable_subjects_for_branch(db, branch_id)
            except Exception as e:
                print(f"[ERROR] Subject loading failed (POST): {repr(e)}")
                subjects = []

        # Re-resolve subject_id for post operation
        resolved_subject_id = subject_id
        subject_id_val = _coerce_int(subject_id)
        if subject_id_val is not None:
            resolved_subject_id = subject_id_val
        elif subject_id and branch_id:
            for s in subjects:
                if s["id"] and subject_name_matches(s["name"], subject_id):
                    resolved_subject_id = s["id"]
                    break
        resolved_subject_id = _coerce_int(resolved_subject_id) or resolved_subject_id

        try:
            timetable_context = _resolve_timetable_slots(db, branch_id, subject_id, selected_date, section=section, period=period)
            print(f"[mark_attendance:POST] Resolved timetable_context slots={len(timetable_context.get('slots', []))} selected_slot={bool(timetable_context.get('selected_slot'))} active_slot={bool(timetable_context.get('active_slot'))} unique={timetable_context.get('unique_slot')}")
        except Exception as exc:
            print(f"[attendance] timetable resolve failed on POST: {repr(exc)}")
            timetable_context = {"slots": [], "selected_slot": None, "active_slot": None, "has_schedule": False, "is_today": False, "current_time": "", "weekday": "", "unique_slot": False}
        selected_slot = timetable_context["selected_slot"]
        valid_periods = {str(item.get("period")) for item in timetable_context["slots"]}

        if not period and selected_slot:
            period = str(selected_slot.get("period") or "")

        if not branch_id or not subject_id:
            flash("Please select a branch and subject.", "error")
        elif not timetable_context["has_schedule"]:
            flash("No scheduled class found for the selected branch, subject, and date.", "error")
        elif selected_date_obj == today_date and not timetable_context["active_slot"] and not timetable_context["unique_slot"]:
            flash("No current active class found right now. Attendance is disabled outside the timetable slot.", "error")
        elif period and period not in valid_periods:
            flash("Selected period does not match the timetable.", "error")
        elif not period and not timetable_context["unique_slot"]:
            flash("Please select a timetable period.", "error")
        else:
            student_ids = request.form.getlist("student_id")
            if branch_id and resolved_subject_id and student_ids:
                saved_student_ids = []
                try:
                    for student_id in student_ids:
                        status = request.form.get(f"status_{student_id}", "Absent")
                        note = request.form.get(f"note_{student_id}", "")
                        existing = db.execute(
                            f"SELECT id FROM attendance WHERE student_id = {placeholder} AND subject_id = {placeholder} AND date = {placeholder}",
                            (student_id, resolved_subject_id, selected_date),
                        ).fetchone()
                        if existing:
                            db.execute(
                                f"UPDATE attendance SET status = {placeholder}, note = {placeholder} WHERE id = {placeholder}",
                                (status, note, row_get(existing, "id")),
                            )
                        else:
                            db.execute(
                                f"INSERT INTO attendance (student_id, branch_id, subject_id, date, status, note, period) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
                                (student_id, branch_id, resolved_subject_id, selected_date, status, note, period),
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
                            subject_id=subject_id,
                            date=selected_date,
                            period=period or (str(selected_slot["period"]) if selected_slot else ""),
                            section=section,
                        )
                    )
                except Exception as e:
                    db.rollback()
                    print(f"Error saving attendance: {e}")
                    flash("Error saving attendance. Please try again.", "error")
            else:
                flash("Please select a branch, subject, and mark attendance for students.", "error")

    attendance_map = {}
    if branch_id and subject_id:
        try:
            rows = db.execute(
                f"SELECT student_id, status, note FROM attendance WHERE subject_id = {placeholder} AND date = {placeholder}",
                (resolved_subject_id, selected_date),
            ).fetchall()
            attendance_map = {str(row["student_id"]): row for row in rows}
        except Exception:
            attendance_map = {}

    if branch_id and subject_id and timetable_context["selected_slot"] and (not timetable_context["is_today"] or timetable_context["active_slot"] or timetable_context["unique_slot"]):
        students = _attendance_students_for_branch(db, branch_id, section)
        if not existing_dates:
            existing_dates = db.execute(
                f"SELECT date, COUNT(*) as count FROM attendance WHERE branch_id = {placeholder} AND subject_id = {placeholder} GROUP BY date ORDER BY date DESC",
                (branch_id, resolved_subject_id),
            ).fetchall()

    selected_period = timetable_context["selected_slot"]
    active_period = timetable_context["active_slot"]
    can_mark_attendance = bool(selected_period and (not timetable_context["is_today"] or active_period or timetable_context["unique_slot"]))
    schedule_message = ""
    if branch_id and subject_id:
        if not timetable_context["has_schedule"]:
            schedule_message = "No scheduled class found"
        elif timetable_context["is_today"] and not active_period and not timetable_context["unique_slot"]:
            schedule_message = "No current active class found"
        elif selected_period:
            schedule_message = "Current Active Class" if selected_period.get("is_active") else "Scheduled Class"

    db.close()
    return render_template(
        "mark_attendance.html",
        branches=branches,
        subjects=subjects,
        students=students,
        branch_id=branch_id,
        subject_id=subject_id,
        section=section,
        selected_date=selected_date,
        period=period,
        timetable_slots=timetable_context["slots"],
        selected_period=selected_period,
        current_active_period=active_period,
        schedule_message=schedule_message,
        can_mark_attendance=can_mark_attendance,
        unique_slot=timetable_context["unique_slot"],
        attendance_map=attendance_map,
        existing_dates=existing_dates,
        prev_date=prev_date,
        next_date=next_date,
        today_date=today_date.isoformat(),
    )


@app.route("/api/current-period")
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
        f"SELECT te.*, COALESCE(s.name, te.subject_name, '') AS subject_name, COALESCE(t.name, te.faculty_name, '') AS teacher_name, te.branch_id "
        f"FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN teachers t ON te.teacher_id = t.id "
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
            sql += f" AND LOWER(TRIM(COALESCE(s.name, te.subject_name, ''))) = LOWER(TRIM({placeholder}))"
            params.append(subject_q)
    sql += " ORDER BY te.start_time LIMIT 1"

    row = db.execute(sql, tuple(params)).fetchone()

    # Fallback: if no active slot, find the next upcoming slot today matching filters
    if not row:
        sql2 = (
            f"SELECT te.*, COALESCE(s.name, te.subject_name, '') AS subject_name, COALESCE(t.name, te.faculty_name, '') AS teacher_name, te.branch_id "
            f"FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN teachers t ON te.teacher_id = t.id "
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
                sql2 += f" AND LOWER(TRIM(COALESCE(s.name, te.subject_name, ''))) = LOWER(TRIM({placeholder}))"
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
        existing = db.execute(
            f"SELECT COUNT(*) AS c FROM attendance WHERE subject_id = {placeholder} AND date = {placeholder}",
            (subject_id, selected_date),
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
@login_required
def api_timetable_subjects():
    branch_id = request.args.get("branch_id") or ""
    section = (request.args.get("section") or "").strip()
    db = None
    try:
        db = get_db()
        subjects = _get_timetable_subjects_for_branch(db, branch_id, section=section) if branch_id else []
        print(f"[api_timetable_subjects] branch={branch_id} section={section} subjects_found={len(subjects)}")
        return jsonify({"subjects": subjects, "count": len(subjects)})
    except Exception as e:
        print(f"[api_timetable_subjects] ERROR: {repr(e)}")
        return jsonify({"subjects": [], "count": 0, "error": str(e)})
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@app.route("/api/timetable-slots")
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
        print(f"[api_timetable_slots] branch={branch_id} section={section} subject={subject_id} slots={len(context.get('slots', []))}")
        return jsonify(context)
    except Exception as e:
        print(f"[api_timetable_slots] ERROR: {repr(e)}")
        return jsonify({"slots": [], "selected_slot": None, "active_slot": None, "has_schedule": False, "is_today": False, "current_time": "", "weekday": "", "unique_slot": False, "error": str(e)})
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@app.route("/api/attendance-periods")
@login_required
def api_attendance_periods():
    branch_id = request.args.get("branch_id") or ""
    subject_id = request.args.get("subject_id") or ""
    selected_date = request.args.get("date") or date.today().isoformat()
    section = (request.args.get("section") or "").strip()
    period = request.args.get("period") or ""
    db = get_db()
    try:
        try:
            context = _resolve_timetable_slots(db, branch_id, subject_id, selected_date, section=section, period=period)
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
                "start_time": s.get("start_time"),
                "end_time": s.get("end_time"),
                "timetable_entry_id": s.get("timetable_entry_id"),
                "subject_name": s.get("subject_name"),
                "is_active": s.get("is_active"),
            }
            for s in ctx.get("slots", [])
        ]
        print(f"[api_timetable_periods] branch={branch_id} section={section} subject={subject_id} periods={len(periods)}")
        return jsonify({"periods": periods, "selected": ctx.get("selected_slot"), "active": ctx.get("active_slot"), "unique": ctx.get("unique_slot"), "has_schedule": ctx.get("has_schedule")})
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
        flash("Timetable module is unavailable right now. Please try again shortly.", "warning")
        return redirect(url_for("dashboard"))


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
