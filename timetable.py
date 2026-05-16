import os
import logging
import sqlite3
import re
from datetime import datetime, time, timezone
from typing import List, Dict, Optional
import traceback
import difflib
import json
import tracemalloc

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

# Tunables for import batching and preview limits
BATCH_INSERT_SIZE = int(os.environ.get("TIMETABLE_BATCH_SIZE", 50))
PREVIEW_ROW_CAP = int(os.environ.get("TIMETABLE_PREVIEW_CAP", 500))

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


def _db_execute(db, query, params=()):
    """Execute a query using the DB connection.

    - Prefer PostgreSQL-style `%s` placeholders in code.
    - If running against SQLite, convert `%s` -> `?` so queries remain compatible.
    - If the module uses `?` placeholders, convert them to `%s` for Postgres.
    This lets callers write `%s` (Postgres-first) but still run on SQLite in dev.
    """
    try:
        is_pg = _is_postgres_db(db)
        if is_pg:
            # Convert legacy sqlite placeholders to %s
            if "?" in query:
                query = query.replace("?", "%s")
        else:
            # SQLite: convert %s to ?
            if "%s" in query:
                # Replace all %s with ? (positional)
                query = query.replace("%s", "?")
        return db.execute(query, params)
    except Exception:
        raise


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


def _clean_text(value) -> str:
    return (str(value).strip() if value is not None else "")


def _table_diagnostics(db):
    """Return duplicate/null diagnostics for timetable tables."""
    result = {}
    try:
        def _safe_fetch(sql):
            try:
                return _db_execute(db, sql).fetchone()
            except Exception:
                return None

        slots_dup = _safe_fetch(
            """
            SELECT COUNT(1) AS c
            FROM (
                SELECT branch, section, day, start_time, end_time, subject_name, faculty_name, room
                FROM timetable_slots
                GROUP BY branch, section, day, start_time, end_time, subject_name, faculty_name, room
                HAVING COUNT(1) > 1
            ) dup
            """
        )
        slots_dup_rows = _safe_fetch(
            """
            SELECT COALESCE(SUM(c), 0) AS c FROM (
                SELECT COUNT(1) AS c
                FROM timetable_slots
                GROUP BY branch, section, day, start_time, end_time, subject_name, faculty_name, room
                HAVING COUNT(1) > 1
            ) dup
            """
        )
        entries_dup = _safe_fetch(
            """
            SELECT COUNT(1) AS c
            FROM (
                SELECT branch_id, section, semester, day, start_time, end_time, subject_id, teacher_id, room
                FROM timetable_entries
                GROUP BY branch_id, section, semester, day, start_time, end_time, subject_id, teacher_id, room
                HAVING COUNT(1) > 1
            ) dup
            """
        )
        entries_dup_rows = _safe_fetch(
            """
            SELECT COALESCE(SUM(c), 0) AS c FROM (
                SELECT COUNT(1) AS c
                FROM timetable_entries
                GROUP BY branch_id, section, semester, day, start_time, end_time, subject_id, teacher_id, room
                HAVING COUNT(1) > 1
            ) dup
            """
        )

        slots_nulls = _safe_fetch(
            """
            SELECT
              COALESCE(SUM(CASE WHEN branch IS NULL OR TRIM(branch) = '' THEN 1 ELSE 0 END), 0) AS branch_null,
              COALESCE(SUM(CASE WHEN section IS NULL OR TRIM(section) = '' THEN 1 ELSE 0 END), 0) AS section_null,
              COALESCE(SUM(CASE WHEN day IS NULL OR TRIM(day) = '' THEN 1 ELSE 0 END), 0) AS day_null,
              COALESCE(SUM(CASE WHEN start_time IS NULL OR TRIM(start_time) = '' THEN 1 ELSE 0 END), 0) AS start_null,
              COALESCE(SUM(CASE WHEN end_time IS NULL OR TRIM(end_time) = '' THEN 1 ELSE 0 END), 0) AS end_null,
              COALESCE(SUM(CASE WHEN subject_name IS NULL OR TRIM(subject_name) = '' THEN 1 ELSE 0 END), 0) AS subject_null,
              COALESCE(SUM(CASE WHEN faculty_name IS NULL OR TRIM(faculty_name) = '' THEN 1 ELSE 0 END), 0) AS faculty_null,
              COALESCE(SUM(CASE WHEN room IS NULL OR TRIM(room) = '' THEN 1 ELSE 0 END), 0) AS room_null
            FROM timetable_slots
            """
        )
        entries_nulls = _safe_fetch(
            """
            SELECT
              COALESCE(SUM(CASE WHEN branch_id IS NULL THEN 1 ELSE 0 END), 0) AS branch_id_null,
              COALESCE(SUM(CASE WHEN section IS NULL OR TRIM(section) = '' THEN 1 ELSE 0 END), 0) AS section_null,
              COALESCE(SUM(CASE WHEN semester IS NULL THEN 1 ELSE 0 END), 0) AS semester_null,
              COALESCE(SUM(CASE WHEN day IS NULL OR TRIM(day) = '' THEN 1 ELSE 0 END), 0) AS day_null,
              COALESCE(SUM(CASE WHEN start_time IS NULL OR TRIM(start_time) = '' THEN 1 ELSE 0 END), 0) AS start_null,
              COALESCE(SUM(CASE WHEN end_time IS NULL OR TRIM(end_time) = '' THEN 1 ELSE 0 END), 0) AS end_null,
              COALESCE(SUM(CASE WHEN subject_id IS NULL THEN 1 ELSE 0 END), 0) AS subject_null,
              COALESCE(SUM(CASE WHEN teacher_id IS NULL THEN 1 ELSE 0 END), 0) AS teacher_null,
              COALESCE(SUM(CASE WHEN room IS NULL OR TRIM(room) = '' THEN 1 ELSE 0 END), 0) AS room_null
            FROM timetable_entries
            """
        )

        result = {
            "slots_duplicate_groups": int(slots_dup[0] if slots_dup else 0),
            "slots_duplicate_rows": int(slots_dup_rows[0] if slots_dup_rows else 0),
            "entries_duplicate_groups": int(entries_dup[0] if entries_dup else 0),
            "entries_duplicate_rows": int(entries_dup_rows[0] if entries_dup_rows else 0),
            "slots_nulls": dict(slots_nulls) if slots_nulls else {},
            "entries_nulls": dict(entries_nulls) if entries_nulls else {},
        }
    except Exception:
        logger.exception("timetable diagnostics failed")
    return result


# --- Simple parsing helpers -------------------------------------------------

def parse_docx_table(path: str) -> List[Dict]:
    """Parse a DOCX timetable file using heuristic: look for tables and extract rows.

    Returns a list of slot dicts with keys: branch, section, semester, day, start_time, end_time, subject_name, faculty_name, is_lab, room
    """
    if docx is None:
        raise RuntimeError("python-docx is not installed. Install with: pip install python-docx")
    doc = docx.Document(path)
    slots = []
    parsed_rows = 0
    skipped_rows = 0
    failed_rows = 0
    try:
        for table_index, table in enumerate(doc.tables):
            if not table.rows:
                logger.info("parse_docx_table table=%s empty", table_index)
                continue
            headers = [_normalize_key(c.text) for c in table.rows[0].cells]
            logger.info("parse_docx_table table=%s rows=%s headers=%s", table_index, len(table.rows), headers)
            for row_index, row in enumerate(table.rows[1:], start=1):
                values = [_clean_text(c.text) for c in row.cells]
                row_text = " | ".join(values)
                row_map = {headers[i]: values[i] for i in range(min(len(headers), len(values))) if headers[i]}
                try:
                    normalized = _normalize_slot_row(row_map, row_text=row_text)
                    subject_raw = normalized["subject_name"]
                    faculty_raw = normalized["faculty_name"]
                    skip_tokens = ("short break", "lunch break", "lib", "sports", "break")
                    if _row_has_token(subject_raw, *skip_tokens) or _row_has_token(faculty_raw, *skip_tokens) or _row_has_token(row_text, *skip_tokens):
                        skipped_rows += 1
                        logger.info("parse_docx_table skipped break row table=%s row=%s text=%s", table_index, row_index, row_text)
                        continue
                    if not _valid_slot_row(normalized):
                        skipped_rows += 1
                        logger.info("parse_docx_table skipped invalid row table=%s row=%s normalized=%s text=%s", table_index, row_index, normalized, row_text)
                        continue

                    subjects = _split_subjects(subject_raw)
                    if not subjects:
                        skipped_rows += 1
                        logger.info("parse_docx_table skipped row with no subject split table=%s row=%s text=%s", table_index, row_index, row_text)
                        continue

                    for subject in subjects:
                        slot = dict(normalized)
                        slot["subject_name"] = _clean_text(subject)
                        slot["is_lab"] = int(bool(_row_has_token(subject, "lab", "practical") or normalized["is_lab"]))
                        if not _valid_slot_row(slot):
                            skipped_rows += 1
                            logger.info("parse_docx_table skipped normalized subject row table=%s row=%s slot=%s text=%s", table_index, row_index, slot, row_text)
                            continue
                        slots.append(slot)
                        parsed_rows += 1
                        logger.info("parse_docx_table parsed row table=%s row=%s slot=%s", table_index, row_index, slot)
                except Exception:
                    failed_rows += 1
                    logger.exception("parse_docx_table failed table=%s row=%s raw=%s", table_index, row_index, row_text)
    except Exception as e:
        logger.exception("parse_docx_table failed")
        raise
    logger.info("parse_docx_table summary: tables=%d parsed_rows=%d skipped_rows=%d failed_rows=%d", len(doc.tables), parsed_rows, skipped_rows, failed_rows)
    return slots


def parse_docx_grid(path: str) -> List[Dict]:
    """Parse grid-style DOCX timetables.

    Expects a table where the first column is the day and the first row
    contains time slots. Attempts to extract subject (and optional faculty)
    from each cell and convert into slot dicts.
    """
    if docx is None:
        raise RuntimeError("python-docx is not installed. Install with: pip install python-docx")
    doc = docx.Document(path)
    slots = []
    # Try to infer branch/section from title or filename
    title_text = " ".join([p.text for p in doc.paragraphs[:3] if p.text])
    base = os.path.splitext(os.path.basename(path))[0]
    inferred_branch = ""
    inferred_section = ""
    # Heuristics: look for patterns like 'B.TECH I-2' or similar
    m = re.search(r"([A-Za-z0-9\.\s&/]+)\s+(I\s*-?\d+|I[-\s]?\d+|II|III|IV|I-\d+)", title_text, flags=re.I)
    if m:
        inferred_branch = m.group(1).strip()
        inferred_section = m.group(2).strip()
    else:
        parts = re.split(r"[_\-\s]+", base)
        if parts:
            inferred_branch = parts[0]
            if len(parts) > 1:
                inferred_section = parts[1]

    for table_index, table in enumerate(doc.tables):
        if not table.rows:
            continue
        # header row (assume first row) contains times starting from column 1
        headers = [_clean_text(c.text) for c in table.rows[0].cells]
        day_col = None
        time_cols = []
        for i, h in enumerate(headers):
            low = h.lower()
            if 'day' in low or 'day/time' in low or 'day time' in low:
                day_col = i
            else:
                # recognize time-like headers by digits or a dash
                if re.search(r"\d", h) or '-' in h:
                    time_cols.append(i)
        if day_col is None:
            day_col = 0
        if not time_cols:
            time_cols = [i for i in range(len(headers)) if i != day_col]

        for row_index, row in enumerate(table.rows[1:], start=1):
            day = _clean_text(row.cells[day_col].text)
            if not day:
                continue
            for col in time_cols:
                try:
                    cell_text = _clean_text(row.cells[col].text)
                except Exception:
                    cell_text = ''
                if not cell_text:
                    continue
                # header time range
                head = _clean_text(table.rows[0].cells[col].text)
                start_time, end_time = _split_time_range(head)
                # split multiple subjects inside the cell
                subjects = _split_subjects(cell_text)
                for subj in subjects:
                    s_text = subj
                    faculty = ""
                    # try to extract faculty if cell has newline or a dash
                    if '\n' in cell_text:
                        lines = [l.strip() for l in cell_text.splitlines() if l.strip()]
                        if len(lines) >= 2:
                            s_text = _clean_text(lines[0])
                            faculty = _clean_text(lines[1])
                    else:
                        # patterns like 'SUBJECT - FACULTY' or 'SUBJECT / FACULTY'
                        parts = re.split(r"[-/\\r\\n]+", subj)
                        if len(parts) >= 2:
                            s_text = _clean_text(parts[0])
                            faculty = _clean_text(parts[1])

                    slot = {
                        'branch': inferred_branch or base,
                        'section': inferred_section or 'ALL',
                        'semester': None,
                        'day': day,
                        'start_time': start_time or '',
                        'end_time': end_time or '',
                        'subject_name': s_text,
                        'faculty_name': faculty,
                        'is_lab': int(bool(_row_has_token(s_text, 'lab', 'practical') or _row_has_token(cell_text, 'lab', 'practical'))),
                        'room': '',
                    }
                    if _valid_slot_row(slot):
                        slots.append(slot)
                    else:
                        # try a relaxed insert by filling branch/section defaults
                        slot['branch'] = slot.get('branch') or base
                        slot['section'] = slot.get('section') or 'ALL'
                        if _valid_slot_row(slot):
                            slots.append(slot)
                        else:
                            logger.info("parse_docx_grid skipped invalid slot: %s", slot)
    logger.info("parse_docx_grid summary: tables=%d parsed_slots=%d", len(doc.tables), len(slots))
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
    parsed_rows = 0
    skipped_rows = 0
    failed_rows = 0

    def _emit_row(row_map: Dict[str, str], raw_text: str, context_label: str):
        nonlocal parsed_rows, skipped_rows, failed_rows
        try:
            normalized = _normalize_slot_row(row_map, row_text=raw_text)
            subject_raw = normalized["subject_name"]
            faculty_raw = normalized["faculty_name"]
            skip_tokens = ("short break", "lunch break", "lib", "sports", "break")
            if _row_has_token(subject_raw, *skip_tokens) or _row_has_token(faculty_raw, *skip_tokens) or _row_has_token(raw_text, *skip_tokens):
                skipped_rows += 1
                logger.info("parse_pdf_to_slots skipped break %s text=%s", context_label, raw_text)
                return
            if not _valid_slot_row(normalized):
                skipped_rows += 1
                logger.info("parse_pdf_to_slots skipped invalid %s normalized=%s text=%s", context_label, normalized, raw_text)
                return
            subjects = _split_subjects(subject_raw)
            if not subjects:
                skipped_rows += 1
                logger.info("parse_pdf_to_slots skipped subjectless %s text=%s", context_label, raw_text)
                return
            for subject in subjects:
                slot = dict(normalized)
                slot["subject_name"] = _clean_text(subject)
                slot["is_lab"] = int(bool(_row_has_token(subject, "lab", "practical") or normalized["is_lab"]))
                if not _valid_slot_row(slot):
                    skipped_rows += 1
                    logger.info("parse_pdf_to_slots skipped normalized subject row %s slot=%s text=%s", context_label, slot, raw_text)
                    continue
                slots.append(slot)
                parsed_rows += 1
                logger.info("parse_pdf_to_slots parsed %s slot=%s", context_label, slot)
        except Exception:
            failed_rows += 1
            logger.exception("parse_pdf_to_slots failed %s raw=%s", context_label, raw_text)

    # Prefer structured extraction from tables embedded in PDFs
    try:
        if pdfplumber is not None:
            with pdfplumber.open(path) as pdf:
                for page_index, page in enumerate(pdf.pages):
                    try:
                        tables = page.extract_tables() or []
                    except Exception:
                        tables = []
                    for table_index, table in enumerate(tables):
                        if not table:
                            continue
                        headers = [_normalize_key(cell) for cell in table[0]]
                        logger.info("parse_pdf_to_slots page=%s table=%s rows=%s headers=%s", page_index, table_index, len(table), headers)
                        for row_index, values in enumerate(table[1:], start=1):
                            raw_values = [_clean_text(v) for v in values]
                            row_text = " | ".join(raw_values)
                            row_map = {headers[i]: raw_values[i] for i in range(min(len(headers), len(raw_values))) if headers[i]}
                            _emit_row(row_map, row_text, f"page={page_index} table={table_index} row={row_index}")
    except Exception:
        logger.exception("parse_pdf_to_slots table extraction failed")

    for ln in lines:
        try:
            parts = [p.strip() for p in re.split(r"\s*[-|]\s*", ln) if p.strip()]
            if len(parts) >= 3:
                day = parts[0]
                time_part = parts[1]
                subj_raw = parts[2]
                fac = parts[3] if len(parts) > 3 else ""
                row_map = {
                    "day": day,
                    "time": time_part,
                    "subject": subj_raw,
                    "faculty": fac,
                }
                _emit_row(row_map, ln, f"line={ln[:80]}")
            else:
                skipped_rows += 1
                logger.info("parse_pdf_to_slots skipped unstructured line=%s", ln)
        except Exception:
            failed_rows += 1
            logger.exception("parse_pdf_to_slots line parse failed: %s", ln)
            continue
    logger.info("parse_pdf_to_slots summary: parsed_rows=%d skipped_rows=%d failed_rows=%d", parsed_rows, skipped_rows, failed_rows)
    return slots


def _row_exists(db, table: str, where_clause: str, params: tuple) -> bool:
    try:
        row = _db_execute(db, f"SELECT 1 FROM {table} WHERE {where_clause} LIMIT 1", params).fetchone()
        return row is not None
    except Exception:
        return False


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean_text(value).lower()).strip()


def _first_non_empty(row_map: Dict[str, str], *aliases: str) -> str:
    for alias in aliases:
        value = _clean_text(row_map.get(_normalize_key(alias), ""))
        if value:
            return value
    return ""


def _row_has_token(text: str, *tokens: str) -> bool:
    haystack = _clean_text(text).lower()
    return any(token.lower() in haystack for token in tokens)


def _split_time_value(time_value: str, end_value: str = ""):
    start_time = _clean_text(time_value)
    end_time = _clean_text(end_value)
    if start_time and not end_time:
        if "-" in start_time:
            return _split_time_range(start_time)
        if "to" in start_time.lower():
            parts = re.split(r"\bto\b", start_time, flags=re.IGNORECASE)
            if len(parts) >= 2:
                return _format_time_str(parts[0].strip()) or "", _format_time_str(parts[1].strip()) or ""
    if not start_time and end_time:
        return "", _format_time_str(end_time) or ""
    if start_time and end_time:
        return _format_time_str(start_time) or "", _format_time_str(end_time) or ""
    if start_time:
        return _split_time_range(start_time)
    return "", ""


def _normalize_slot_row(row: Dict[str, str], row_text: str = "") -> Dict[str, str]:
    normalized = {key: _clean_text(value) for key, value in row.items()}
    normalized["branch"] = _first_non_empty(normalized, "branch", "dept", "department", "program", "course", "branch name")
    normalized["section"] = _first_non_empty(normalized, "section", "class", "division", "batch", "group")
    semester_value = _first_non_empty(normalized, "semester", "sem", "term", "year")
    normalized["semester"] = _safe_int(semester_value)
    normalized["day"] = _first_non_empty(normalized, "day", "weekday", "date")
    time_value = _first_non_empty(normalized, "time", "slot", "period", "session")
    start_value = _first_non_empty(normalized, "start", "start time", "from", "begin")
    end_value = _first_non_empty(normalized, "end", "end time", "to", "until", "finish")
    if not start_value and not end_value and time_value:
        start_value, end_value = _split_time_range(time_value)
    else:
        start_value, end_value = _split_time_value(start_value or time_value, end_value)
    normalized["start_time"] = start_value
    normalized["end_time"] = end_value
    normalized["subject_name"] = _first_non_empty(normalized, "subject", "course", "paper", "topic", "title")
    normalized["faculty_name"] = _first_non_empty(normalized, "faculty", "teacher", "instructor", "lecturer", "staff")
    normalized["room"] = _first_non_empty(normalized, "room", "classroom", "hall", "venue", "lab room")
    normalized["is_lab"] = int(bool(_row_has_token(normalized["subject_name"], "lab", "practical") or _row_has_token(row_text, "lab", "practical")))
    return normalized


def _valid_slot_row(row: Dict[str, str]) -> bool:
    required = ("branch", "section", "day", "start_time", "end_time", "subject_name")
    return all(_clean_text(row.get(field)) for field in required)


def _split_subjects(subj_raw: str):
    """Split merged subject strings into individual subject names.

    Examples: 'PYTHON/AEP LAB' -> ['PYTHON LAB', 'AEP LAB']
    """
    if not subj_raw:
        return [""]
    s = subj_raw.strip()
    # normalize separators
    parts = [p.strip() for p in re.split(r"[/,&]", s) if p.strip()]
    results = []
    for p in parts:
        # if original had LAB suffix, preserve it
        if re.search(r"\blab\b", s, flags=re.IGNORECASE) and not re.search(r"\blab\b", p, flags=re.IGNORECASE):
            results.append((p + " LAB").strip())
        else:
            results.append(p)
    return results


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
        row = _db_execute(db, "SELECT id FROM branches WHERE LOWER(name)=LOWER(?) LIMIT 1", (branch_name,)).fetchone()
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
            rows = _db_execute(db,
                "SELECT te.*, s.name AS subject_name, t.name AS teacher_name, b.name AS branch_name FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN teachers t ON te.teacher_id = t.id LEFT JOIN branches b ON te.branch_id = b.id WHERE te.branch_id = ? AND COALESCE(te.section, '') = ? AND te.day = ? AND te.start_time >= ? ORDER BY te.start_time LIMIT ?",
                (branch_id, section or "", weekday, cur_time, limit),
            ).fetchall()
            entries = [_row_to_dict(r) for r in rows]
    except Exception:
        entries = []

    if not entries:
        try:
            rows = _db_execute(db,
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
        rows = _db_execute(db,
            "SELECT te.*, s.name AS subject_name, t.name AS teacher_name, b.name AS branch_name FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN teachers t ON te.teacher_id = t.id LEFT JOIN branches b ON te.branch_id = b.id WHERE te.day = ? AND te.start_time <= ? AND te.end_time >= ? ORDER BY te.start_time LIMIT 1",
            (weekday, cur_time, cur_time),
        ).fetchall()
        if rows:
            return _row_to_dict(rows[0])
    except Exception:
        pass
    try:
        rows = _db_execute(db,
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
        rows = _db_execute(db,
            "SELECT te.*, s.name AS subject_name, b.name AS branch_name, t.name AS teacher_name FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN branches b ON te.branch_id = b.id LEFT JOIN teachers t ON te.teacher_id = t.id WHERE te.teacher_id = ? AND te.day = ? ORDER BY te.start_time",
            (teacher_id, weekday),
        ).fetchall()
    except Exception:
        rows = []

    if rows:
        return [_row_to_dict(r) for r in rows]

    try:
        rows = _db_execute(db,
            "SELECT * FROM timetable_slots WHERE faculty_name IS NOT NULL AND day = ? ORDER BY start_time",
            (weekday,),
        ).fetchall()
    except Exception:
        rows = []
    return [_row_to_dict(r) for r in rows]


# --- DB import --------------------------------------------------------------

def import_slots(db, slots: List[Dict]):
    """Import raw timetable slots into `timetable_slots` with verbose diagnostics.

    Returns a dict: {"counters": {...}, "skipped_rows": [...]}
    """
    counters = {
        "total": len(slots),
        "parsed": 0,
        "inserted": 0,
        "skipped_total": 0,
        "skipped_invalid": 0,
        "skipped_duplicate": 0,
        "failures": 0,
        "invalid_section": 0,
        "normalization_failures": 0,
    }
    skipped_rows = []
    preview_path = os.path.join(os.path.dirname(__file__), "uploads", "last_import_debug.jsonl")
    os.makedirs(os.path.dirname(preview_path), exist_ok=True)
    preview_written = 0
    batch_commit_counts = 0
    progress_log_interval = max(50, BATCH_INSERT_SIZE)
    start_time = time.time()
    use_tracemalloc = False
    try:
        tracemalloc.start()
        use_tracemalloc = True
    except Exception:
        use_tracemalloc = False

    try:
        logger.info("import_slots: parsed_rows_count=%d", len(slots))
        inserted_since_commit = 0
        for row_index, s in enumerate(slots, start=1):
            counters["parsed"] += 1
            row = {
                "branch": _clean_text(s.get("branch")),
                "section": _clean_text(s.get("section")),
                "semester": _safe_int(s.get("semester")),
                "day": _clean_text(s.get("day")),
                "start_time": _clean_text(s.get("start_time")),
                "end_time": _clean_text(s.get("end_time")),
                "subject_name": _clean_text(s.get("subject_name")),
                "faculty_name": _clean_text(s.get("faculty_name")),
                "is_lab": int(bool(s.get("is_lab"))),
                "room": _clean_text(s.get("room")),
            }

            logger.info("Processing row %s: raw=%s normalized=%s", row_index, s, row)

            if not _valid_slot_row(row):
                counters["skipped_total"] += 1
                counters["skipped_invalid"] += 1
                reason = "invalid_row"
                if not row.get("section"):
                    counters["invalid_section"] += 1
                    reason = "missing_section"
                logger.info("import_slots skipped row %s: reason=%s normalized=%s", row_index, reason, row)
                # stream skipped row to preview file (capped)
                if preview_written < PREVIEW_ROW_CAP:
                    try:
                        with open(preview_path, "a", encoding="utf-8") as pf:
                            pf.write(json.dumps({"index": row_index, "raw": s, "normalized": row, "reason": reason}, default=str) + "\n")
                        preview_written += 1
                    except Exception:
                        logger.exception("Failed to write skipped preview line")
                # keep a small in-memory sample for immediate return
                if len(skipped_rows) < 20:
                    skipped_rows.append({"index": row_index, "raw": s, "normalized": row, "reason": reason})
                continue

            duplicate_where = "COALESCE(branch, '') = COALESCE(%s, '') AND COALESCE(section, '') = COALESCE(%s, '') AND COALESCE(CAST(semester AS TEXT), '') = COALESCE(CAST(%s AS TEXT), '') AND COALESCE(day, '') = COALESCE(%s, '') AND COALESCE(start_time, '') = COALESCE(%s, '') AND COALESCE(end_time, '') = COALESCE(%s, '') AND COALESCE(subject_name, '') = COALESCE(%s, '') AND COALESCE(faculty_name, '') = COALESCE(%s, '') AND COALESCE(room, '') = COALESCE(%s, '')"
            try:
                if _row_exists(
                    db,
                    "timetable_slots",
                    duplicate_where,
                    (row['branch'], row['section'], row['semester'], row['day'], row['start_time'], row['end_time'], row['subject_name'], row['faculty_name'], row['room']),
                ):
                    counters["skipped_total"] += 1
                    counters["skipped_duplicate"] += 1
                    reason = "duplicate"
                    logger.info("import_slots skipped duplicate row %s: %s", row_index, row)
                    skipped_rows.append({"index": row_index, "raw": s, "normalized": row, "reason": reason})
                    continue
            except Exception:
                counters["normalization_failures"] += 1
                logger.exception("Duplicate check failed at row %s | row=%s", row_index, row)
                logger.error(traceback.format_exc())
                skipped_rows.append({"index": row_index, "raw": s, "normalized": row, "reason": "dup_check_exception"})
                raise

            try:
                _db_execute(
                    db,
                    "INSERT INTO timetable_slots (branch, section, semester, day, start_time, end_time, subject_name, faculty_name, is_lab, room) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (row['branch'], row['section'], row['semester'], row['day'], row['start_time'], row['end_time'], row['subject_name'], row['faculty_name'], row['is_lab'], row['room']),
                )
                counters["inserted"] += 1
                inserted_since_commit += 1
                # commit in batches to reduce transaction memory
                if inserted_since_commit >= BATCH_INSERT_SIZE:
                    try:
                        db.commit()
                        batch_commit_counts += 1
                        # write a lightweight batch commit diagnostic line
                        try:
                            with open(preview_path, "a", encoding="utf-8") as pf:
                                pf.write(json.dumps({"type": "batch_commit", "batch_count": inserted_since_commit, "inserted_total": counters.get("inserted"), "timestamp": time.time()}, default=str) + "\n")
                        except Exception:
                            logger.exception("Failed to write batch commit diagnostic")
                    except Exception:
                        logger.exception("DB commit failed during batch commit in import_slots")
                    inserted_since_commit = 0
                # periodic progress log
                if row_index % progress_log_interval == 0:
                    logger.info("import_slots progress: processed=%d inserted=%d skipped=%d", row_index, counters.get("inserted"), counters.get("skipped_total"))
            except Exception:
                counters["failures"] += 1
                logger.exception("Import failed at row %s | row=%s", row_index, row)
                logger.error(traceback.format_exc())
                if preview_written < PREVIEW_ROW_CAP:
                    try:
                        with open(preview_path, "a", encoding="utf-8") as pf:
                            pf.write(json.dumps({"index": row_index, "raw": s, "normalized": row, "reason": "insert_exception"}, default=str) + "\n")
                        preview_written += 1
                    except Exception:
                        logger.exception("Failed to write insert_exception preview line")
                if len(skipped_rows) < 20:
                    skipped_rows.append({"index": row_index, "raw": s, "normalized": row, "reason": "insert_exception"})
                raise

        try:
            db.commit()
            if inserted_since_commit > 0:
                batch_commit_counts += 1
                try:
                    with open(preview_path, "a", encoding="utf-8") as pf:
                        pf.write(json.dumps({"type": "batch_commit", "batch_count": inserted_since_commit, "inserted_total": counters.get("inserted"), "timestamp": time.time()}, default=str) + "\n")
                except Exception:
                    logger.exception("Failed to write final batch commit diagnostic")
        except Exception:
            logger.exception("DB commit failed after import_slots")
            logger.error(traceback.format_exc())
            raise

    except Exception:
        logger.exception("import_slots failed")
        logger.error(traceback.format_exc())
        raise

    logger.info(
        "import_slots summary: %s",
        {k: counters.get(k) for k in ("total", "parsed", "inserted", "skipped_total", "skipped_invalid", "skipped_duplicate", "failures", "invalid_section", "normalization_failures")},
    )
    # capture peak memory if tracemalloc was used
    peak = None
    try:
        if use_tracemalloc:
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
    except Exception:
        peak = None

    return {"counters": counters, "skipped_rows": skipped_rows, "preview_path": preview_path if preview_written else None, "batch_commits": batch_commit_counts, "elapsed_seconds": time.time() - start_time, "memory_peak_bytes": peak}


def import_slots_normalized(db, slots: List[Dict]):
    """Insert slots into normalized `timetable_entries` table when possible.
    Best-effort: resolve branch -> branches.id, subject -> subjects.id, faculty -> teachers.id.
    Falls back to leaving subject_id/teacher_id NULL if resolution fails.
    Returns a dict: {"counters": {...}, "skipped_rows": [...]}
    """
    counters = {
        "total": len(slots),
        "parsed": 0,
        "inserted": 0,
        "skipped_total": 0,
        "skipped_branch": 0,
        "skipped_unresolved_subject": 0,
        "skipped_unresolved_teacher": 0,
        "skipped_invalid": 0,
        "skipped_duplicate": 0,
        "failures": 0,
        "missing_subjects": 0,
        "missing_teachers": 0,
        "invalid_section": 0,
        "normalization_failures": 0,
    }
    skipped_rows = []
    preview_path = os.path.join(os.path.dirname(__file__), "uploads", "last_import_debug.jsonl")
    os.makedirs(os.path.dirname(preview_path), exist_ok=True)
    preview_written = 0
    batch_commit_counts = 0
    progress_log_interval = max(50, BATCH_INSERT_SIZE)
    start_time = time.time()
    use_tracemalloc = False
    try:
        tracemalloc.start()
        use_tracemalloc = True
    except Exception:
        use_tracemalloc = False
    try:
        inserted_since_commit = 0
        for row_index, s in enumerate(slots, start=1):
            counters["parsed"] += 1
            bname = _clean_text(s.get("branch"))
            sec = _clean_text(s.get("section"))
            sem = _safe_int(s.get("semester"))
            day = _clean_text(s.get("day"))
            start = _clean_text(s.get("start_time"))
            end = _clean_text(s.get("end_time"))
            subj_name = _clean_text(s.get("subject_name"))
            fac_name = _clean_text(s.get("faculty_name"))
            is_lab = int(bool(s.get("is_lab")))
            room = _clean_text(s.get("room"))

            normalized_row = {
                "branch": bname,
                "section": sec,
                "semester": sem,
                "day": day,
                "start_time": start,
                "end_time": end,
                "subject_name": subj_name,
                "faculty_name": fac_name,
                "is_lab": is_lab,
                "room": room,
            }
            logger.info("Processing normalized row %s: raw=%s normalized=%s", row_index, s, normalized_row)
            if not _valid_slot_row({"branch": bname, "section": sec, "day": day, "start_time": start, "end_time": end, "subject_name": subj_name}):
                counters["skipped_total"] += 1
                counters["skipped_invalid"] += 1
                counters["invalid_section"] += 1
                reason = "invalid_row"
                logger.info("import_slots_normalized skipped row %s: reason=%s normalized=%s", row_index, reason, normalized_row)
                if preview_written < PREVIEW_ROW_CAP:
                    try:
                        with open(preview_path, "a", encoding="utf-8") as pf:
                            pf.write(json.dumps({"index": row_index, "raw": s, "normalized": normalized_row, "reason": reason}, default=str) + "\n")
                        preview_written += 1
                    except Exception:
                        logger.exception("Failed to write normalized skipped preview line")
                if len(skipped_rows) < 20:
                    skipped_rows.append({"index": row_index, "raw": s, "normalized": normalized_row, "reason": reason})
                continue
            try:
                _db_execute(
                    db,
                    "INSERT INTO timetable_entries (branch_id, section, semester, day, start_time, end_time, subject_id, teacher_id, is_lab, room) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (row['branch_id'], row['section'], row['semester'], row['day'], row['start_time'], row['end_time'], row['subject_id'], row['teacher_id'], row['is_lab'], row['room']),
                )
                counters["inserted"] += 1
                inserted_since_commit += 1
                if inserted_since_commit >= BATCH_INSERT_SIZE:
                    try:
                        db.commit()
                        batch_commit_counts += 1
                        try:
                            with open(preview_path, "a", encoding="utf-8") as pf:
                                pf.write(json.dumps({"type": "batch_commit_normalized", "batch_count": inserted_since_commit, "inserted_total": counters.get("inserted"), "timestamp": time.time()}, default=str) + "\n")
                        except Exception:
                            logger.exception("Failed to write normalized batch commit diagnostic")
                    except Exception:
                        logger.exception("DB commit failed during batch commit in import_slots_normalized")
                    inserted_since_commit = 0
                if row_index % progress_log_interval == 0:
                    logger.info("import_slots_normalized progress: processed=%d inserted=%d skipped=%d", row_index, counters.get("inserted"), counters.get("skipped_total"))
            except Exception:
                counters["failures"] += 1
                counters["normalization_failures"] += 1
                logger.exception("Import failed at normalized row %s | row=%s", row_index, normalized_row)
                logger.error(traceback.format_exc())
                if preview_written < PREVIEW_ROW_CAP:
                    try:
                        with open(preview_path, "a", encoding="utf-8") as pf:
                            pf.write(json.dumps({"index": row_index, "raw": s, "normalized": normalized_row, "reason": "insert_exception"}, default=str) + "\n")
                        preview_written += 1
                    except Exception:
                        logger.exception("Failed to write normalized insert_exception preview line")
                if len(skipped_rows) < 20:
                    skipped_rows.append({"index": row_index, "raw": s, "normalized": normalized_row, "reason": "insert_exception"})
                raise

            teacher_id = None
            try:
                row = _db_execute(db, "SELECT id FROM teachers WHERE LOWER(name)=LOWER(%s) LIMIT 1", (fac_name,)).fetchone()
                teacher_id = row[0] if row and not hasattr(row, 'keys') else (row['id'] if row else None)
            except Exception:
                teacher_id = None

            if branch_id is None:
                counters["skipped_total"] += 1
                counters["skipped_branch"] += 1
                reason = "missing_branch"
                logger.info("import_slots_normalized skipped unresolved branch for row %s: %s", row_index, normalized_row)
                skipped_rows.append({"index": row_index, "raw": s, "normalized": normalized_row, "reason": reason})
                continue

            if subject_id is None:
                counters["skipped_unresolved_subject"] += 1
                counters["missing_subjects"] += 1
            if teacher_id is None:
                counters["skipped_unresolved_teacher"] += 1
                counters["missing_teachers"] += 1

            if subject_id is None or teacher_id is None:
                logger.info(
                    "import_slots_normalized inserting with unresolved subject/teacher branch=%s subject=%s teacher=%s row_index=%s",
                    branch_id,
                    subject_id,
                    teacher_id,
                    row_index,
                )

            row = {
                'branch_id': branch_id,
                'section': sec,
                'semester': sem,
                'day': day,
                'start_time': start or "",
                'end_time': end or "",
                'subject_id': subject_id,
                'teacher_id': teacher_id,
                'is_lab': is_lab,
                'room': room,
            }
            logger.info("import_slots_normalized inserting (row_index=%s): %s", row_index, row)
            duplicate_where = "COALESCE(CAST(branch_id AS TEXT), '') = COALESCE(CAST(%s AS TEXT), '') AND COALESCE(section, '') = COALESCE(%s, '') AND COALESCE(CAST(semester AS TEXT), '') = COALESCE(CAST(%s AS TEXT), '') AND COALESCE(day, '') = COALESCE(%s, '') AND COALESCE(start_time, '') = COALESCE(%s, '') AND COALESCE(end_time, '') = COALESCE(%s, '') AND COALESCE(CAST(subject_id AS TEXT), '') = COALESCE(CAST(%s AS TEXT), '') AND COALESCE(CAST(teacher_id AS TEXT), '') = COALESCE(CAST(%s AS TEXT), '') AND COALESCE(room, '') = COALESCE(%s, '')"
            try:
                if _row_exists(
                    db,
                    "timetable_entries",
                    duplicate_where,
                    (row['branch_id'], row['section'], row['semester'], row['day'], row['start_time'], row['end_time'], row['subject_id'], row['teacher_id'], row['room']),
                ):
                    counters["skipped_total"] += 1
                    counters["skipped_duplicate"] += 1
                    reason = "duplicate"
                    logger.info("import_slots_normalized skipped duplicate row %s: %s", row_index, row)
                    skipped_rows.append({"index": row_index, "raw": s, "normalized": normalized_row, "reason": reason})
                    continue
            except Exception:
                counters["normalization_failures"] += 1
                logger.exception("Duplicate check failed at row %s | row=%s", row_index, normalized_row)
                logger.error(traceback.format_exc())
                skipped_rows.append({"index": row_index, "raw": s, "normalized": normalized_row, "reason": "dup_check_exception"})
                raise

            try:
                logger.info(
                    "Normalized values | subject=%s teacher=%s section=%s day=%s time=%s-%s",
                    subj_name,
                    fac_name,
                    sec,
                    day,
                    start,
                    end,
                )
                _db_execute(
                    db,
                    "INSERT INTO timetable_entries (branch_id, section, semester, day, start_time, end_time, subject_id, teacher_id, is_lab, room) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (row['branch_id'], row['section'], row['semester'], row['day'], row['start_time'], row['end_time'], row['subject_id'], row['teacher_id'], row['is_lab'], row['room']),
                )
                counters["inserted"] += 1
            except Exception:
                counters["failures"] += 1
                counters["normalization_failures"] += 1
                logger.exception("Import failed at normalized row %s | row=%s", row_index, normalized_row)
                logger.error(traceback.format_exc())
                skipped_rows.append({"index": row_index, "raw": s, "normalized": normalized_row, "reason": "insert_exception"})
                raise

        try:
            db.commit()
            if inserted_since_commit > 0:
                batch_commit_counts += 1
                try:
                    with open(preview_path, "a", encoding="utf-8") as pf:
                        pf.write(json.dumps({"type": "batch_commit_normalized", "batch_count": inserted_since_commit, "inserted_total": counters.get("inserted"), "timestamp": time.time()}, default=str) + "\n")
                except Exception:
                    logger.exception("Failed to write final normalized batch commit diagnostic")
        except Exception:
            logger.exception("DB commit failed after import_slots_normalized")
            logger.error(traceback.format_exc())
            raise

    except Exception:
        logger.exception("import_slots_normalized failed")
        logger.error(traceback.format_exc())
        raise

    logger.info(
        "import_slots_normalized summary: %s",
        {k: counters.get(k) for k in ("total", "parsed", "inserted", "skipped_total", "skipped_branch", "skipped_unresolved_subject", "skipped_unresolved_teacher", "skipped_invalid", "skipped_duplicate", "failures", "missing_subjects", "missing_teachers", "invalid_section", "normalization_failures")},
    )
    peak = None
    try:
        if use_tracemalloc:
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
    except Exception:
        peak = None

    return {"counters": counters, "skipped_rows": skipped_rows, "preview_path": preview_path if preview_written else None, "batch_commits": batch_commit_counts, "elapsed_seconds": time.time() - start_time, "memory_peak_bytes": peak}


# --- Lookup helpers --------------------------------------------------------

def get_current_slot(db, branch: str, section: str, now: Optional[datetime] = None):
    if now is None:
        now = datetime.now()
    weekday = _clean_text(now.strftime("%A"))
    cur_time = _clean_text(now.strftime("%H:%M"))
    branch = _clean_text(branch)
    section = _clean_text(section)
    logger.info("get_current_slot start branch=%s section=%s day=%s time=%s", branch, section, weekday, cur_time)
    # Prefer normalized timetable_entries when available (uses branch_id)
    try:
        branch_row = _db_execute(db, "SELECT id FROM branches WHERE LOWER(TRIM(name))=LOWER(TRIM(%s)) LIMIT 1", (branch,)).fetchone()
        branch_id = branch_row[0] if branch_row else None
    except Exception:
        branch_id = None
    logger.info("get_current_slot resolved branch_id=%s", branch_id)

    if branch_id is not None:
        rows = _db_execute(db,
            "SELECT te.*, s.name AS subject_name, t.name AS teacher_name FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN teachers t ON te.teacher_id = t.id WHERE te.branch_id = %s AND LOWER(TRIM(COALESCE(te.section, ''))) = LOWER(TRIM(%s)) AND LOWER(TRIM(COALESCE(te.day, ''))) = LOWER(TRIM(%s)) AND te.start_time <= %s AND te.end_time >= %s ORDER BY te.start_time LIMIT 1",
            (branch_id, section or "", weekday, cur_time, cur_time),
        ).fetchall()
        if rows:
            logger.info("get_current_slot matched normalized timetable_entries row_id=%s subject=%s teacher=%s", rows[0]["id"] if hasattr(rows[0], "keys") and "id" in rows[0].keys() else None, rows[0]["subject_name"] if hasattr(rows[0], "keys") and "subject_name" in rows[0].keys() else None, rows[0]["teacher_name"] if hasattr(rows[0], "keys") and "teacher_name" in rows[0].keys() else None)
            return rows[0]

    # Fallback to legacy timetable_slots text-based lookup
    rows = _db_execute(db,
        "SELECT * FROM timetable_slots WHERE LOWER(TRIM(branch)) = LOWER(TRIM(%s)) AND LOWER(TRIM(COALESCE(section, ''))) = LOWER(TRIM(%s)) AND LOWER(TRIM(day)) = LOWER(TRIM(%s)) AND start_time <= %s AND end_time >= %s ORDER BY start_time LIMIT 1",
        (branch, section, weekday, cur_time, cur_time),
    ).fetchall()
    if rows:
        logger.info("get_current_slot matched legacy timetable_slots row subject=%s faculty=%s", rows[0]["subject_name"] if hasattr(rows[0], "keys") and "subject_name" in rows[0].keys() else None, rows[0]["faculty_name"] if hasattr(rows[0], "keys") and "faculty_name" in rows[0].keys() else None)
        return rows[0]
    logger.info("get_current_slot no active slot found for branch=%s section=%s day=%s time=%s", branch, section, weekday, cur_time)
    return None


# --- Routes registration ---------------------------------------------------

def register_routes(app, db_getter=None):
    globals()["get_db"] = db_getter

    @app.route("/timetable")
    def timetable_home():
        db = None
        rows_count = 0
        table_ready = False
        upcoming_classes = []
        try:
            if db_getter is None:
                raise RuntimeError("Database getter is not configured")
            db = db_getter()
            ensure_timetable_tables(db)
            row = _db_execute(db, "SELECT COUNT(1) AS c FROM timetable_slots").fetchone()
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
            # Support delete action
            if request.form.get("action") == "delete_timetable":
                try:
                    db = get_db()
                    _db_execute(db, "DELETE FROM timetable_entries")
                    _db_execute(db, "DELETE FROM timetable_slots")
                    try:
                        db.commit()
                    except Exception:
                        pass
                    flash("Timetable deleted successfully.", "success")
                except Exception:
                    logger.exception("Failed to delete timetable data")
                    flash("Failed to delete timetable.", "error")
                return redirect(url_for("timetable_manage"))

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

                if not slots and ext in (".docx",) and docx is not None:
                    # try grid-style DOCX parsing as a fallback
                    try:
                        slots = parse_docx_grid(dest)
                    except Exception:
                        logger.exception("parse_docx_grid failed")

                if not slots:
                    logger.warning("Timetable import parsed zero rows from file=%s ext=%s", filename, ext)
                    flash(
                        "No timetable rows were parsed from the uploaded file. Check that the DOCX/PDF contains a readable table with branch, section, day, time, and subject columns.",
                        "error",
                    )
                    return redirect(url_for("timetable_manage"))

                inserted_info = import_slots(db, slots)
                normalized_info = import_slots_normalized(db, slots)

                # Persist a temporary preview of skipped rows for admin review
                preview = {
                    "raw_insert": inserted_info,
                    "normalized_insert": normalized_info,
                }
                try:
                    preview_path = os.path.join(os.path.dirname(__file__), "uploads", "last_import_debug.json")
                    with open(preview_path, "w", encoding="utf-8") as f:
                        import json
                        json.dump(preview, f, indent=2, default=str)
                except Exception:
                    logger.exception("Failed to write import debug preview")

                i_c = inserted_info.get("counters", {}) if isinstance(inserted_info, dict) else {}
                n_c = normalized_info.get("counters", {}) if isinstance(normalized_info, dict) else {}
                flash(
                    f"Imported slots: inserted={i_c.get('inserted', 0)} skipped={i_c.get('skipped_total', 0)}. Normalized: inserted={n_c.get('inserted', 0)} skipped={n_c.get('skipped_total', 0)}. Preview file written.",
                    "success",
                )
            except Exception as e:
                logger.exception("Failed to import timetable")
                flash(f"Failed to import timetable: {e}", "error")
            return redirect(url_for("timetable_manage"))

        # GET: show simple management UI
        rows = _db_execute(db, "SELECT * FROM timetable_slots ORDER BY day, start_time").fetchall()
        skipped_preview = None
        try:
            preview_path = os.path.join(os.path.dirname(__file__), "uploads", "last_import_debug.json")
            if os.path.exists(preview_path):
                with open(preview_path, "r", encoding="utf-8") as f:
                    skipped_preview = f.read()
        except Exception:
            logger.exception("Failed to load skipped preview")
        # show normalized preview when available
        try:
            entries = _db_execute(db, "SELECT te.*, s.name AS subject_name, t.name AS teacher_name, b.name AS branch_name FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN teachers t ON te.teacher_id = t.id LEFT JOIN branches b ON te.branch_id = b.id ORDER BY te.day, te.start_time").fetchall()
        except Exception:
            entries = []
        return render_template("timetable_manage.html", rows=rows, entries=entries, skipped_preview=skipped_preview)

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

    @app.route("/timetable/admin/bulk_resolve", methods=("POST",))
    def timetable_admin_bulk_resolve():
        if session.get("role") != "admin":
            return redirect(url_for("dashboard"))
        db = get_db()
        create_missing = request.form.get("create_missing_subjects") in ("1", "true", "True")
        summary = {
            "subjects_checked": 0,
            "subjects_created": 0,
            "subjects_mapped": 0,
            "teachers_checked": 0,
            "teachers_mapped": 0,
        }
        try:
            # Gather distinct subject names
            rows = _db_execute(db, "SELECT DISTINCT TRIM(subject_name) AS subject_name FROM timetable_slots WHERE subject_name IS NOT NULL").fetchall()
            slot_subjects = [r[0] if not hasattr(r, 'keys') else r.get('subject_name') for r in rows if r and (r[0] if not hasattr(r, 'keys') else r.get('subject_name'))]
            rows = _db_execute(db, "SELECT DISTINCT TRIM(subject_name) AS subject_name FROM timetable_entries WHERE subject_name IS NOT NULL").fetchall()
            entry_subjects = [r[0] if not hasattr(r, 'keys') else r.get('subject_name') for r in rows if r and (r[0] if not hasattr(r, 'keys') else r.get('subject_name'))]
            distinct = sorted(set([_clean_text(s) for s in (slot_subjects + entry_subjects) if s]))

            existing = _db_execute(db, "SELECT id, name FROM subjects").fetchall()
            existing_map = { (r[1].lower() if not hasattr(r, 'keys') else r.get('name').lower()): (r[0] if not hasattr(r, 'keys') else r.get('id')) for r in existing }
            existing_names = list(existing_map.keys())

            # default branch for created subjects
            row = _db_execute(db, "SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()
            default_branch_id = (row[0] if not hasattr(row, 'keys') else row.get('id')) if row else None

            for name in distinct:
                if not name:
                    continue
                summary["subjects_checked"] += 1
                lname = name.lower()
                if lname in existing_map:
                    summary["subjects_mapped"] += 1
                    continue
                # fuzzy match to existing subjects
                match = difflib.get_close_matches(name, existing_names, n=1, cutoff=0.8)
                if match:
                    matched_lower = match[0]
                    existing_map[lname] = existing_map[matched_lower]
                    summary["subjects_mapped"] += 1
                    continue
                if create_missing:
                    display = name.title()
                    try:
                        _db_execute(db, "INSERT INTO subjects (name, branch_id) VALUES (%s, %s)", (display, default_branch_id))
                        try:
                            db.commit()
                        except Exception:
                            pass
                        row = _db_execute(db, "SELECT id FROM subjects WHERE name = %s LIMIT 1", (display,)).fetchone()
                        sid = (row[0] if not hasattr(row, 'keys') else row.get('id')) if row else None
                        if sid:
                            existing_map[lname] = sid
                            existing_names.append(lname)
                            summary["subjects_created"] += 1
                    except Exception:
                        logger.exception("failed to create subject %s", display)

            # Apply mappings: update timetable_entries subject_id where subject_name matches
            for raw_lower, sid in list(existing_map.items()):
                if not sid:
                    continue
                try:
                    _db_execute(db, "UPDATE timetable_entries SET subject_id = %s WHERE subject_id IS NULL AND LOWER(TRIM(subject_name)) = LOWER(TRIM(%s))", (sid, raw_lower))
                except Exception:
                    logger.exception("failed to update timetable_entries for subject %s", raw_lower)

            # Teachers: normalize names and attempt to map
            rows = _db_execute(db, "SELECT DISTINCT TRIM(faculty_name) AS faculty_name FROM timetable_slots WHERE faculty_name IS NOT NULL").fetchall()
            slot_teachers = [r[0] if not hasattr(r, 'keys') else r.get('faculty_name') for r in rows if r and (r[0] if not hasattr(r, 'keys') else r.get('faculty_name'))]
            rows = _db_execute(db, "SELECT DISTINCT TRIM(faculty_name) AS faculty_name FROM timetable_entries WHERE faculty_name IS NOT NULL").fetchall()
            entry_teachers = [r[0] if not hasattr(r, 'keys') else r.get('faculty_name') for r in rows if r and (r[0] if not hasattr(r, 'keys') else r.get('faculty_name'))]
            tdistinct = sorted(set([_clean_text(t) for t in (slot_teachers + entry_teachers) if t]))
            existing_t = _db_execute(db, "SELECT id, name FROM teachers").fetchall()
            existing_t_map = { (r[1].lower() if not hasattr(r, 'keys') else r.get('name').lower()): (r[0] if not hasattr(r, 'keys') else r.get('id')) for r in existing_t }
            existing_t_names = list(existing_t_map.keys())

            def normalize_teacher_name(n):
                if not n:
                    return ""
                t = re.sub(r'^(mr|ms|mrs|dr)\.?\s+', '', n, flags=re.I)
                t = re.sub(r'[^\w\s]', ' ', t)
                return ' '.join(t.split()).strip()

            for tname in tdistinct:
                if not tname:
                    continue
                summary["teachers_checked"] += 1
                norm = normalize_teacher_name(tname)
                ln = norm.lower()
                if ln in existing_t_map:
                    summary["teachers_mapped"] += 1
                    tid = existing_t_map[ln]
                    try:
                        _db_execute(db, "UPDATE timetable_entries SET teacher_id = %s WHERE teacher_id IS NULL AND LOWER(TRIM(faculty_name)) = LOWER(TRIM(%s))", (tid, tname))
                    except Exception:
                        logger.exception("failed to update teacher mapping for %s", tname)
                    continue
                match = difflib.get_close_matches(norm, existing_t_names, n=1, cutoff=0.85)
                if match:
                    matched = match[0]
                    tid = existing_t_map[matched]
                    summary["teachers_mapped"] += 1
                    try:
                        _db_execute(db, "UPDATE timetable_entries SET teacher_id = %s WHERE teacher_id IS NULL AND LOWER(TRIM(faculty_name)) = LOWER(TRIM(%s))", (tid, tname))
                    except Exception:
                        logger.exception("failed to update teacher mapping for %s", tname)

            try:
                db.commit()
            except Exception:
                pass
            flash(f"Bulk resolve completed: {summary}", "success")
        except Exception as e:
            logger.exception("bulk_resolve failed")
            flash(f"Bulk resolve failed: {e}", "error")
        return redirect(url_for("timetable_manage"))

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
                rows = _db_execute(
                    db,
                    "SELECT te.*, s.name AS subject_name, t.name AS teacher_name, b.name AS branch_name FROM timetable_entries te LEFT JOIN subjects s ON te.subject_id = s.id LEFT JOIN teachers t ON te.teacher_id = t.id LEFT JOIN branches b ON te.branch_id = b.id ORDER BY te.day, te.start_time",
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
            rows = _db_execute(
                db,
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
                    sub_row = _db_execute(db, "SELECT id FROM subjects WHERE LOWER(name)=LOWER(?) LIMIT 1", (subject_name,)).fetchone()
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
            students = _db_execute(db, "SELECT id, name FROM students WHERE branch_id = ? AND (COALESCE(section, '') = COALESCE(?, '')) ORDER BY name", (branch_id, section or "")).fetchall()
        else:
            # fallback to previous name-based lookup
            students = _db_execute(db, "SELECT s.id, s.name FROM students s JOIN branches b ON s.branch_id = b.id WHERE b.name = ? AND s.section = ? ORDER BY s.name", (branch, section)).fetchall()

        # Use today's date and default period=1 for current slot marking
        from datetime import date as _d
        today_str = _d.today().isoformat()
        period = 1

        # Prevent duplicate attendance for the same subject today+period
        duplicate_check = False
        try:
            if subject_id:
                dup_row = _db_execute(db, "SELECT COUNT(1) AS c FROM attendance WHERE subject_id = ? AND date = ? AND period = ?", (subject_id, today_str, period)).fetchone()
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
                    _db_execute(
                        db,
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
                    _db_execute(
                        db,
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

    @app.route("/health/timetable")
    def timetable_health():
        """Health-check for timetable initialization and DB compatibility.

        Access: admin users OR non-production/debug OR Render internal hosts.
        Returns JSON describing status, counts, and postgres compatibility.
        """
        # Access control: allow only admin or non-prod/debug or Render internal
        try:
            is_admin = bool(session.get("role") == "admin")
        except Exception:
            is_admin = False

        allow_internal = bool(app.debug or os.environ.get("RENDER") or os.environ.get("RENDER_INTERNAL_HOSTNAME"))
        if not (is_admin or allow_internal):
            return jsonify({"ok": False, "error": "unauthorized"}), 403

        result = {"ok": False, "postgres_compatible": False, "tables": {}, "messages": []}
        db = None
        try:
            if db_getter is None:
                raise RuntimeError("Database getter is not configured")
            db = db_getter()

            # Verify connectivity
            try:
                # simple query to ensure connection is alive
                cur = _db_execute(db, "SELECT 1")
                _ = cur.fetchone()
                result["messages"].append("db_connectivity_ok")
            except Exception:
                result["messages"].append("db_connectivity_failed")
                raise

            # Ensure tables are present (this may create them)
            ensure_timetable_tables(db)
            result["messages"].append("ensure_timetable_tables_executed")

            # Postgres compatibility flag
            pg_ok = _is_postgres_db(db)
            result["postgres_compatible"] = pg_ok
            if pg_ok:
                result["messages"].append("postgres_compatibility_detected")

            # Counts
            try:
                r1 = _db_execute(db, "SELECT COUNT(1) AS c FROM timetable_slots").fetchone()
                r2 = _db_execute(db, "SELECT COUNT(1) AS c FROM timetable_entries").fetchone()
                slots_count = int(r1[0] if r1 is not None and r1[0] is not None else 0)
                entries_count = int(r2[0] if r2 is not None and r2[0] is not None else 0)
                result["tables"]["timetable_slots"] = slots_count
                result["tables"]["timetable_entries"] = entries_count
                result["messages"].append("counts_retrieved")
                diagnostics = _table_diagnostics(db)
                result["diagnostics"] = diagnostics
                result["messages"].append("diagnostics_retrieved")
            except Exception:
                # If counts failed, attempt information_schema check for Postgres
                try:
                    if _is_postgres_db(db):
                        q = "SELECT to_regclass('public.timetable_slots') IS NOT NULL AS slots_exists, to_regclass('public.timetable_entries') IS NOT NULL AS entries_exists"
                        rr = _db_execute(db, q).fetchone()
                        result["tables"]["timetable_slots_exists"] = bool(rr[0])
                        result["tables"]["timetable_entries_exists"] = bool(rr[1])
                        result["messages"].append("schema_presence_checked")
                except Exception:
                    result["messages"].append("counts_unavailable")

            result["ok"] = True
            result["messages"].append("timetable_ok")
            # Log summary server-side
            logger.info("Timetable tables verified")
            if result.get("tables"):
                logger.info(f"Timetable schema initialized: slots={result['tables'].get('timetable_slots')} entries={result['tables'].get('timetable_entries')}")
            if result.get("diagnostics"):
                logger.info(
                    "Timetable diagnostics: slots_dup_groups=%s slots_dup_rows=%s entries_dup_groups=%s entries_dup_rows=%s",
                    result["diagnostics"].get("slots_duplicate_groups"),
                    result["diagnostics"].get("slots_duplicate_rows"),
                    result["diagnostics"].get("entries_duplicate_groups"),
                    result["diagnostics"].get("entries_duplicate_rows"),
                )
                logger.info("Timetable null diagnostics: slots=%s entries=%s", result["diagnostics"].get("slots_nulls"), result["diagnostics"].get("entries_nulls"))
            if pg_ok:
                logger.info("PostgreSQL timetable compatibility OK")

            return jsonify(result)
        except Exception as e:
            logger.exception("timetable health check failed")
            msg = {"ok": False, "error": str(type(e).__name__) + ": " + str(e)}
            # include non-sensitive diagnostics
            msg.update({k: v for k, v in result.items() if k in ("postgres_compatible", "tables", "messages")})
            return jsonify(msg), 500
        finally:
            if db:
                try:
                    db.close()
                except Exception:
                    pass


# Register when imported into app.py
try:
    from flask import session
    # plugin-style: if app is imported as module, require explicit registration
except Exception:
    pass
