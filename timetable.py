import os
import logging
import sqlite3
from datetime import datetime, time, timezone
from typing import List, Dict, Optional

from flask import request, redirect, url_for, render_template, flash, jsonify, session, render_template_string

# Parsers are optional imports - provide helpful messages if missing
try:
    import docx
except Exception:
    docx = None

try:
    import pdfplumber
except Exception:
    pdfplumber = None

logger = logging.getLogger("app.timetable")

# Database helper functions - use existing app.get_db() pattern where called from app.py

def _is_postgres_db(db) -> bool:
    try:
        # The app wraps psycopg2 connections in a helper class named _PostgresDB.
        # Fallback: check for a psycopg2 connection module on an underlying conn.
        name = type(db).__name__
        if name == "_PostgresDB":
            return True
        # Some call sites may pass a raw psycopg2 connection or cursor-like object
        if hasattr(db, "_conn"):
            mod = type(db._conn).__module__
            return "psycopg2" in mod
        return False
    except Exception:
        return False


def _create_tables_sql(db):
    if _is_postgres_db(db):
        return [
            """
            CREATE TABLE IF NOT EXISTS timetable_slots (
                id SERIAL PRIMARY KEY,
                branch TEXT NOT NULL,
                section TEXT NOT NULL,
                semester INTEGER,
                day TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                subject_name TEXT NOT NULL,
                faculty_name TEXT,
                is_lab INTEGER DEFAULT 0,
                room TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """,
        ]
    else:
        return [
            """
            CREATE TABLE IF NOT EXISTS timetable_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch TEXT NOT NULL,
                section TEXT NOT NULL,
                semester INTEGER,
                day TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                subject_name TEXT NOT NULL,
                faculty_name TEXT,
                is_lab INTEGER DEFAULT 0,
                room TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """,
        ]


def _create_normalized_sql(db):
    if _is_postgres_db(db):
        return [
            """
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
            """,
        ]
    else:
        return [
            """
            CREATE TABLE IF NOT EXISTS timetable_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            """,
        ]


def ensure_timetable_tables(db):
    for sql in _create_tables_sql(db):
        try:
            db.execute(sql)
        except Exception:
            # ignore individual table creation errors; caller will handle
            logger.exception("create table failed")
    for sql in _create_normalized_sql(db):
        try:
            db.execute(sql)
        except Exception:
            logger.exception("create normalized table failed")
    try:
        db.commit()
    except Exception:
        pass


# --- Simple parsing helpers -------------------------------------------------

def parse_docx_table(path: str) -> List[Dict]:
    """Parse a DOCX timetable file using heuristic: look for tables and extract rows.

    Returns a list of slot dicts with keys: branch, section, semester, day, start_time, end_time, subject_name, faculty_name, is_lab, room
    """
    if docx is None:
        raise RuntimeError("python-docx is not installed. Install with: pip install python-docx")
    doc = docx.Document(path)
    slots = []
    for table in doc.tables:
        # Very generic: iterate rows, assume header in first row describing columns
        headers = [c.text.strip().lower() for c in table.rows[0].cells]
        for row in table.rows[1:]:
            values = [c.text.strip() for c in row.cells]
            row_map = {}
            for h, v in zip(headers, values):
                row_map[h] = v
            # Build best-effort mapping
            slot = {
                "branch": row_map.get("branch") or row_map.get("dept") or "",
                "section": row_map.get("section") or row_map.get("class") or "",
                "semester": _safe_int(row_map.get("semester")),
                "day": row_map.get("day") or row_map.get("weekday") or "",
                "start_time": _normalize_time(row_map.get("time") or row_map.get("start") or ""),
                "end_time": _normalize_time(row_map.get("end") or ""),
                "subject_name": row_map.get("subject") or row_map.get("course") or "",
                "faculty_name": row_map.get("faculty") or row_map.get("teacher") or "",
                "is_lab": 1 if ("lab" in (row_map.get("subject") or "").lower() or (row_map.get("type") or "").lower().strip() == "lab") else 0,
                "room": row_map.get("room") or "",
            }
            slots.append(slot)
    return slots


def parse_pdf_text(path: str) -> str:
    if pdfplumber is None:
        raise RuntimeError("pdfplumber is not installed. Install with: pip install pdfplumber")
    text = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text.append(page.extract_text() or "")
    return "\n".join(text)


def parse_pdf_to_slots(path: str) -> List[Dict]:
    text = parse_pdf_text(path)
    # Heuristic parsing: look for lines that contain day/time/subject
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    slots = []
    for ln in lines:
        # naive pattern: DAY TIME - SUBJECT - FACULTY
        parts = [p.strip() for p in ln.split("-")]
        if len(parts) >= 3:
            day = parts[0]
            time_part = parts[1]
            subj = parts[2]
            fac = parts[3] if len(parts) > 3 else ""
            st, et = _split_time_range(time_part)
            slots.append({
                "branch": "",
                "section": "",
                "semester": None,
                "day": day,
                "start_time": st,
                "end_time": et,
                "subject_name": subj,
                "faculty_name": fac,
                "is_lab": 1 if "lab" in subj.lower() else 0,
                "room": "",
            })
    return slots


# --- Utilities --------------------------------------------------------------

def _safe_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


def _normalize_time(val: str) -> str:
    if not val:
        return ""
    val = val.strip()
    # Accept formats like 09:00-10:00 or 09.00 - 10.00 or 9 AM - 10 AM
    if "-" in val:
        a, b = val.split("-", 1)
        return _format_time_str(a.strip()) or "", _format_time_str(b.strip()) or ""
    # Single time -> treat as start
    return _format_time_str(val)


def _format_time_str(s: str) -> Optional[str]:
    s = s.strip()
    # Try HH:MM
    for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%H.%M"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%H:%M")
        except Exception:
            continue
    # Numeric hour
    try:
        h = int(s)
        return f"{h:02d}:00"
    except Exception:
        return None


def _split_time_range(s: str):
    if "-" in s:
        a, b = s.split("-", 1)
        return _format_time_str(a.strip()) or "", _format_time_str(b.strip()) or ""
    return (_format_time_str(s) or "", "")


def _current_local_datetime(now: Optional[datetime] = None) -> datetime:
    """Return a timezone-aware local datetime for timetable comparisons."""
    if now is None:
        return datetime.now().astimezone()
    if getattr(now, "tzinfo", None) is None:
        return now.astimezone()
    return now.astimezone()


def _row_to_dict(row):
    try:
        return dict(row)
    except Exception:
        return row


def _lookup_branch_id(db, branch_name: str):
    if not branch_name:
        return None
    try:
        row = db.execute("SELECT id FROM branches WHERE LOWER(name)=LOWER(?) LIMIT 1", (branch_name,)).fetchone()
        if row:
            return row[0] if not hasattr(row, "keys") else row["id"]
    except Exception:
        return None
    return None


def get_upcoming_classes(db, branch: str = "", section: str = "", limit: int = 3, now: Optional[datetime] = None):
    """Return upcoming classes for the current day using normalized data first."""
    current = _current_local_datetime(now)
    weekday = current.strftime("%A")
    cur_time = current.strftime("%H:%M")
    branch_id = _lookup_branch_id(db, branch)

    entries = []
    try:
        if branch_id is not None:
            rows = db.execute(
                "SELECT te.*, s.name AS subject_name, t.name AS teacher_name, b.name AS branch_name FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN teachers t ON te.teacher_id = t.id LEFT JOIN branches b ON te.branch_id = b.id WHERE te.branch_id = ? AND COALESCE(te.section, '') = ? AND te.day = ? AND te.start_time >= ? ORDER BY te.start_time LIMIT ?",
                (branch_id, section or "", weekday, cur_time, limit),
            ).fetchall()
            entries = [_row_to_dict(r) for r in rows]
    except Exception:
        entries = []

    if not entries:
        try:
            rows = db.execute(
                "SELECT * FROM timetable_slots WHERE branch = ? AND section = ? AND day = ? AND start_time >= ? ORDER BY start_time LIMIT ?",
                (branch or "", section or "", weekday, cur_time, limit),
            ).fetchall()
            entries = [_row_to_dict(r) for r in rows]
        except Exception:
            entries = []

    return entries


def get_current_active_class(db, branch: str = "", section: str = "", now: Optional[datetime] = None):
    """Return the currently running class, preferring normalized timetable_entries."""
    return get_current_slot(db, branch, section, now=now)


def get_global_active_class(db, now: Optional[datetime] = None):
    """Return the first active class across the institute for the current time."""
    current = _current_local_datetime(now)
    weekday = current.strftime("%A")
    cur_time = current.strftime("%H:%M")
    try:
        rows = db.execute(
            "SELECT te.*, s.name AS subject_name, t.name AS teacher_name, b.name AS branch_name FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN teachers t ON te.teacher_id = t.id LEFT JOIN branches b ON te.branch_id = b.id WHERE te.day = ? AND te.start_time <= ? AND te.end_time >= ? ORDER BY te.start_time LIMIT 1",
            (weekday, cur_time, cur_time),
        ).fetchall()
        if rows:
            return _row_to_dict(rows[0])
    except Exception:
        pass
    try:
        rows = db.execute(
            "SELECT * FROM timetable_slots WHERE day = ? AND start_time <= ? AND end_time >= ? ORDER BY start_time LIMIT 1",
            (weekday, cur_time, cur_time),
        ).fetchall()
        if rows:
            return _row_to_dict(rows[0])
    except Exception:
        pass
    return None


def get_faculty_schedule(db, teacher_id, now: Optional[datetime] = None):
    current = _current_local_datetime(now)
    weekday = current.strftime("%A")
    rows = []
    try:
        rows = db.execute(
            "SELECT te.*, s.name AS subject_name, b.name AS branch_name, t.name AS teacher_name FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN branches b ON te.branch_id = b.id LEFT JOIN teachers t ON te.teacher_id = t.id WHERE te.teacher_id = ? AND te.day = ? ORDER BY te.start_time",
            (teacher_id, weekday),
        ).fetchall()
    except Exception:
        rows = []

    if rows:
        return [_row_to_dict(r) for r in rows]

    try:
        rows = db.execute(
            "SELECT * FROM timetable_slots WHERE faculty_name IS NOT NULL AND day = ? ORDER BY start_time",
            (weekday,),
        ).fetchall()
    except Exception:
        rows = []
    return [_row_to_dict(r) for r in rows]


# --- DB import --------------------------------------------------------------

def import_slots(db, slots: List[Dict]):
    placeholder = "?"
    if str(db).startswith("<"):
        # can't detect; assume sqlite3.Connection
        placeholder = "?"
    inserted = 0
    for s in slots:
        start = s.get("start_time")
        end = s.get("end_time")
        if isinstance(start, tuple):
            # from docx parser when returning tuple
            st, et = start
        else:
            st = start if isinstance(start, str) else ""
            et = end if isinstance(end, str) else ""
        db.execute(
            """
            INSERT INTO timetable_slots (branch, section, semester, day, start_time, end_time, subject_name, faculty_name, is_lab, room)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (s.get("branch") or "", s.get("section") or "", s.get("semester"), s.get("day") or "", st or "", et or "", s.get("subject_name") or "", s.get("faculty_name") or "", int(bool(s.get("is_lab"))), s.get("room") or ""),
        )
        inserted += 1
    try:
        db.commit()
    except Exception:
        pass
    return inserted


def import_slots_normalized(db, slots: List[Dict]):
    """Insert slots into normalized `timetable_entries` table when possible.
    Best-effort: resolve branch -> branches.id, subject -> subjects.id, faculty -> teachers.id.
    Falls back to leaving subject_id/teacher_id NULL if resolution fails.
    Returns number inserted.
    """
    placeholder = "?"
    inserted = 0
    for s in slots:
        start = s.get("start_time")
        end = s.get("end_time")
        if isinstance(start, tuple):
            st, et = start
        else:
            st = start if isinstance(start, str) else ""
            et = end if isinstance(end, str) else ""

        bname = (s.get("branch") or "").strip()
        sec = (s.get("section") or "").strip()
        sem = s.get("semester")
        day = (s.get("day") or "").strip()
        subj_name = (s.get("subject_name") or "").strip()
        fac_name = (s.get("faculty_name") or "").strip()
        is_lab = int(bool(s.get("is_lab")))
        room = (s.get("room") or "").strip()

        branch_id = None
        try:
            row = db.execute("SELECT id FROM branches WHERE LOWER(name)=LOWER(?) LIMIT 1", (bname,)).fetchone()
            branch_id = row[0] if row else None
        except Exception:
            branch_id = None

        subject_id = None
        try:
            row = db.execute("SELECT id FROM subjects WHERE LOWER(name)=LOWER(?) LIMIT 1", (subj_name,)).fetchone()
            subject_id = row[0] if row else None
        except Exception:
            subject_id = None

        teacher_id = None
        try:
            row = db.execute("SELECT id FROM teachers WHERE LOWER(name)=LOWER(?) LIMIT 1", (fac_name,)).fetchone()
            teacher_id = row[0] if row else None
        except Exception:
            teacher_id = None

        try:
            db.execute(
                "INSERT INTO timetable_entries (branch_id, section, semester, day, start_time, end_time, subject_id, teacher_id, is_lab, room) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (branch_id, sec, sem, day, st or "", et or "", subject_id, teacher_id, is_lab, room),
            )
            inserted += 1
        except Exception:
            pass
    try:
        db.commit()
    except Exception:
        pass
    return inserted


# --- Lookup helpers --------------------------------------------------------

def get_current_slot(db, branch: str, section: str, now: Optional[datetime] = None):
    if now is None:
        now = datetime.now()
    weekday = now.strftime("%A")
    cur_time = now.strftime("%H:%M")
    placeholder = "?"
    # Prefer normalized timetable_entries when available (uses branch_id)
    try:
        branch_row = db.execute("SELECT id FROM branches WHERE LOWER(name)=LOWER(?) LIMIT 1", (branch,)).fetchone()
        branch_id = branch_row[0] if branch_row else None
    except Exception:
        branch_id = None

    if branch_id is not None:
        rows = db.execute(
            "SELECT te.*, s.name AS subject_name, t.name AS teacher_name FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN teachers t ON te.teacher_id = t.id WHERE te.branch_id = ? AND COALESCE(te.section, '') = ? AND te.day = ? AND te.start_time <= ? AND te.end_time >= ? ORDER BY te.start_time LIMIT 1",
            (branch_id, section or "", weekday, cur_time, cur_time),
        ).fetchall()
        if rows:
            return rows[0]

    # Fallback to legacy timetable_slots text-based lookup
    rows = db.execute(
        "SELECT * FROM timetable_slots WHERE branch = ? AND section = ? AND day = ? AND start_time <= ? AND end_time >= ? ORDER BY start_time LIMIT 1",
        (branch, section, weekday, cur_time, cur_time),
    ).fetchall()
    if rows:
        return rows[0]
    return None


# --- Routes registration ---------------------------------------------------

def register_routes(app, db_getter=None):
    globals()["get_db"] = db_getter

    @app.route("/timetable")
    def timetable_home():
        db = None
        rows_count = 0
        table_ready = False
        active_slot = None
        upcoming_classes = []
        try:
            if db_getter is None:
                raise RuntimeError("Database getter is not configured")
            db = db_getter()
            ensure_timetable_tables(db)
            row = db.execute("SELECT COUNT(1) AS c FROM timetable_slots").fetchone()
            rows_count = int(row["c"] if row and row["c"] is not None else 0)
            table_ready = True
            active_slot = get_current_active_class(db, session.get("teacher_branch_name") or session.get("teacher_branch") or "", session.get("teacher_section") or "")
            upcoming_classes = get_upcoming_classes(db, session.get("teacher_branch_name") or session.get("teacher_branch") or "", session.get("teacher_section") or "", limit=4)
        except Exception as e:
            logger.exception("Failed to render timetable status page")
            return render_template_string(
                """
                {% extends "layout.html" %}
                {% block content %}
                <div class="card">
                    <h1>Timetable Status</h1>
                    <p>Timetable routes are loaded, but the database check failed.</p>
                    <p>{{ error }}</p>
                    <p><a class="button" href="{{ url_for('timetable_manage') }}">Open timetable management</a></p>
                </div>
                {% endblock %}
                """,
                error=str(e),
            )
        finally:
            if db:
                try:
                    db.close()
                except Exception:
                    pass

        return render_template(
            "timetable_dashboard.html",
            table_ready=table_ready,
            rows_count=rows_count,
            active_slot=active_slot,
            upcoming_classes=upcoming_classes,
        )

    @app.route("/timetable/dashboard")
    def timetable_dashboard_alias():
        return timetable_home()

    @app.route("/timetable/upload")
    def timetable_upload():
        return redirect(url_for("timetable_manage"))
    @app.route("/timetable/manage", methods=("GET", "POST"))
    def timetable_manage():
        if session.get("role") != "admin":
            return redirect(url_for("dashboard"))
        db = get_db()
        ensure_timetable_tables(db)
        if request.method == "POST":
            file = request.files.get("timetable_file")
            if not file:
                flash("Please upload a file.", "error")
                return redirect(url_for("timetable_manage"))
            filename = file.filename or "upload"
            safe_path = os.path.join(os.path.dirname(__file__), "uploads")
            os.makedirs(safe_path, exist_ok=True)
            dest = os.path.join(safe_path, filename)
            file.save(dest)
            # Parse file
            ext = os.path.splitext(filename)[1].lower()
            try:
                if ext in (".docx",) and docx is not None:
                    slots = parse_docx_table(dest)
                elif ext in (".pdf",) and pdfplumber is not None:
                    slots = parse_pdf_to_slots(dest)
                else:
                    flash("Unsupported file type or missing parser dependencies.", "error")
                    return redirect(url_for("timetable_manage"))
                inserted = import_slots(db, slots)
                # Also populate normalized timetable_entries where possible
                try:
                    n_inserted = import_slots_normalized(db, slots)
                except Exception:
                    n_inserted = 0
                flash(f"Imported {inserted} timetable slots. Normalized entries created: {n_inserted}.", "success")
            except Exception as e:
                logger.exception("Failed to import timetable")
                flash(f"Failed to import timetable: {e}", "error")
            return redirect(url_for("timetable_manage"))

        # GET: show simple management UI
        rows = db.execute("SELECT * FROM timetable_slots ORDER BY day, start_time").fetchall()
        # show normalized preview when available
        try:
            entries = db.execute("SELECT te.*, s.name AS subject_name, t.name AS teacher_name, b.name AS branch_name FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN teachers t ON te.teacher_id = t.id LEFT JOIN branches b ON te.branch_id = b.id ORDER BY te.day, te.start_time").fetchall()
        except Exception:
            entries = []
        return render_template("timetable_manage.html", rows=rows, entries=entries)

    @app.route("/timetable/active")
    def timetable_active():
        # Returns the current active class for the logged-in teacher or for query params
        branch = request.args.get("branch") or session.get("teacher_branch_name") or ""
        section = request.args.get("section") or session.get("teacher_section") or ""
        db = get_db()
        ensure_timetable_tables(db)
        row = get_current_slot(db, branch, section)
        if not row:
            return jsonify({"active": False})
        return jsonify({
            "active": True,
            "slot": _row_to_dict(row)
        })

    @app.route("/timetable/faculty-schedules")
    def timetable_faculty_schedules():
        if session.get("role") not in ("admin", "teacher"):
            return redirect(url_for("login"))
        db = get_db()
        ensure_timetable_tables(db)
        teacher_id = session.get("teacher_id") if session.get("role") == "teacher" else None
        schedules = []
        if teacher_id:
            schedules = get_faculty_schedule(db, teacher_id)
        else:
            try:
                rows = db.execute(
                    "SELECT te.*, s.name AS subject_name, t.name AS teacher_name, b.name AS branch_name FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN teachers t ON te.teacher_id = t.id LEFT JOIN branches b ON te.branch_id = b.id ORDER BY te.day, te.start_time"
                ).fetchall()
                schedules = [_row_to_dict(r) for r in rows]
            except Exception:
                schedules = []
        return render_template("faculty_schedules.html", schedules=schedules, teacher_id=teacher_id)

    @app.route('/timetable/students')
    def timetable_students():
        # Return students for a given branch and section (or teacher session)
        branch = request.args.get('branch') or session.get('teacher_branch_name') or ''
        section = request.args.get('section') or session.get('teacher_section') or ''
        db = get_db()
        try:
            rows = db.execute(
                "SELECT s.id, s.name, s.roll_no FROM students s JOIN branches b ON s.branch_id = b.id WHERE b.name = ? AND s.section = ? ORDER BY s.name",
                (branch, section),
            ).fetchall()
            students = [{"id": r["id"], "name": r["name"], "roll_no": r["roll_no"]} for r in rows]
            return jsonify({"ok": True, "students": students})
        except Exception as e:
            logger.exception('failed to fetch students')
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/attendance/mark_current", methods=("POST",))
    def attendance_mark_current():
        # Mark attendance for current slot (teacher must be logged in)
        if session.get("role") != "teacher":
            return jsonify({"ok": False, "error": "unauthorized"}), 403
        data = request.get_json() or {}
        action = data.get("action")  # present / absent / bulk_absent
        branch = session.get("teacher_branch_name") or request.args.get("branch")
        section = session.get("teacher_section") or request.args.get("section")
        db = get_db()
        slot = get_current_slot(db, branch, section)
        if not slot:
            return jsonify({"ok": False, "error": "no active slot"}), 400

        # Determine subject_id / teacher_id / branch_id from normalized entry when present
        try:
            # slot may be sqlite3.Row or dict-like
            subject_id = slot.get("subject_id") if isinstance(slot, dict) else (slot["subject_id"] if "subject_id" in slot.keys() else None)
        except Exception:
            subject_id = None

        # Resolve subject_id if not present but subject_name is available
        if not subject_id:
            try:
                subject_name = slot.get("subject_name") if isinstance(slot, dict) else (slot["subject_name"] if "subject_name" in slot.keys() else None)
            except Exception:
                subject_name = None
            try:
                if subject_name:
                    sub_row = db.execute("SELECT id FROM subjects WHERE LOWER(name)=LOWER(?) LIMIT 1", (subject_name,)).fetchone()
                    if sub_row:
                        subject_id = sub_row[0] if isinstance(sub_row, tuple) or isinstance(sub_row, list) else sub_row["id"]
            except Exception:
                subject_id = None

        # Determine branch_id and students list
        try:
            branch_id = slot.get("branch_id") if isinstance(slot, dict) else (slot["branch_id"] if "branch_id" in slot.keys() else None)
        except Exception:
            branch_id = None

        if branch_id:
            students = db.execute("SELECT id, name FROM students WHERE branch_id = ? AND (COALESCE(section, '') = COALESCE(?, '')) ORDER BY name", (branch_id, section or "")).fetchall()
        else:
            # fallback to previous name-based lookup
            students = db.execute("SELECT s.id, s.name FROM students s JOIN branches b ON s.branch_id = b.id WHERE b.name = ? AND s.section = ? ORDER BY s.name", (branch, section)).fetchall()

        # Use today's date and default period=1 for current slot marking
        from datetime import date as _d
        today_str = _d.today().isoformat()
        period = 1

        # Prevent duplicate attendance for the same subject today+period
        duplicate_check = False
        try:
            if subject_id:
                dup_row = db.execute("SELECT COUNT(1) AS c FROM attendance WHERE subject_id = ? AND date = ? AND period = ?", (subject_id, today_str, period)).fetchone()
                dup_count = dup_row[0] if dup_row is not None else 0
                if int(dup_count) > 0:
                    duplicate_check = True
        except Exception:
            duplicate_check = False

        if duplicate_check:
            return jsonify({"ok": False, "error": "attendance already recorded for this subject today"}), 400

        teacher_id = session.get("teacher_id")

        if action == "bulk_absent":
            marked = 0
            for s in students:
                sid = s[0] if not isinstance(s, dict) else s.get("id")
                try:
                    db.execute(
                        "INSERT INTO attendance (student_id, branch_id, branch_section, section, subject_id, teacher_id, subject_name, date, period, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (sid, branch_id or None, None, section or None, subject_id or None, teacher_id or None, (slot.get("subject_name") if isinstance(slot, dict) else None), today_str, period, "Absent"),
                    )
                    marked += 1
                except Exception:
                    pass
            try:
                db.commit()
            except Exception:
                pass
            return jsonify({"ok": True, "marked": marked})
        elif action in ("present", "absent"):
            student_ids = data.get("student_ids", [])
            marked = 0
            for sid in student_ids:
                st = "Present" if action == "present" else "Absent"
                try:
                    db.execute(
                        "INSERT INTO attendance (student_id, branch_id, branch_section, section, subject_id, teacher_id, subject_name, date, period, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (sid, branch_id or None, None, section or None, subject_id or None, teacher_id or None, (slot.get("subject_name") if isinstance(slot, dict) else None), today_str, period, st),
                    )
                    marked += 1
                except Exception:
                    pass
            try:
                db.commit()
            except Exception:
                pass
            return jsonify({"ok": True, "marked": marked})
        else:
            return jsonify({"ok": False, "error": "unknown action"}), 400

    logger.info("Timetable routes registered")


# Register when imported into app.py
try:
    from flask import session
    # plugin-style: if app is imported as module, require explicit registration
except Exception:
    pass
