import os
import logging
import sqlite3
from datetime import datetime, time
from typing import List, Dict, Optional

from flask import request, redirect, url_for, render_template, flash, jsonify

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

CREATE_TABLES_SQL = [
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


def ensure_timetable_tables(db):
    for sql in CREATE_TABLES_SQL:
        db.execute(sql)
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


# --- Lookup helpers --------------------------------------------------------

def get_current_slot(db, branch: str, section: str, now: Optional[datetime] = None):
    if now is None:
        now = datetime.now()
    weekday = now.strftime("%A")
    cur_time = now.strftime("%H:%M")
    placeholder = "?"
    rows = db.execute(
        "SELECT * FROM timetable_slots WHERE branch = ? AND section = ? AND day = ? AND start_time <= ? AND end_time >= ? ORDER BY start_time LIMIT 1",
        (branch, section, weekday, cur_time, cur_time),
    ).fetchall()
    if rows:
        return rows[0]
    return None


# --- Routes registration ---------------------------------------------------

def register_routes(app):
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
                flash(f"Imported {inserted} timetable slots.", "success")
            except Exception as e:
                logger.exception("Failed to import timetable")
                flash(f"Failed to import timetable: {e}", "error")
            return redirect(url_for("timetable_manage"))

        # GET: show simple management UI
        rows = db.execute("SELECT * FROM timetable_slots ORDER BY day, start_time").fetchall()
        return render_template("timetable_manage.html", rows=rows)

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
            "slot": dict(row)
        })

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
        # Load students
        placeholder = get_placeholder()
        students = db.execute(f"SELECT id, name FROM students WHERE branch_id = (SELECT id FROM branches WHERE name = {placeholder}) AND section = {placeholder}", (branch, section)).fetchall()
        if action == "bulk_absent":
            # Mark all absent for this slot
            for s in students:
                db.execute("INSERT INTO attendance (student_id, subject_id, status, timestamp) VALUES (?, ?, ?, datetime('now'))", (row_get(s, "id"), None, "Absent"))
            db.commit()
            return jsonify({"ok": True, "marked": len(students)})
        elif action in ("present", "absent"):
            student_ids = data.get("student_ids", [])
            marked = 0
            for sid in student_ids:
                st = "Present" if action == "present" else "Absent"
                db.execute("INSERT INTO attendance (student_id, subject_id, status, timestamp) VALUES (?, ?, ?, datetime('now'))", (sid, None, st))
                marked += 1
            db.commit()
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
