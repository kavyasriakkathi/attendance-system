import os
import logging
import sqlite3
import re
import time
import gc
from datetime import datetime, timezone
from typing import List, Dict, Optional, Iterator, Iterable
import traceback
import difflib
import json
import tracemalloc
import zipfile
import xml.etree.ElementTree as ET

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
BATCH_INSERT_SIZE = int(os.environ.get("TIMETABLE_BATCH_SIZE", 5))
PREVIEW_ROW_CAP = int(os.environ.get("TIMETABLE_PREVIEW_CAP", 0))
SKIPPED_ROW_SAMPLE_CAP = int(os.environ.get("TIMETABLE_SKIPPED_SAMPLE_CAP", 5))
TIMETABLE_MAX_TABLES = int(os.environ.get("TIMETABLE_MAX_TABLES", 20))
TIMETABLE_SINGLE_SECTION_ONLY = os.environ.get("TIMETABLE_SINGLE_SECTION_ONLY", "true").strip().lower() in ("1", "true", "yes", "on")
ENABLE_IMPORT_TRACEMALLOC = os.environ.get("TIMETABLE_ENABLE_TRACEMALLOC", "false").strip().lower() in ("1", "true", "yes", "on")
PDF_DIAG_SAMPLE_CAP = int(os.environ.get("TIMETABLE_PDF_DIAG_SAMPLE_CAP", 12))
PDF_TABLE_SETTINGS = (
    {
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",
        "intersection_tolerance": 5,
        "snap_tolerance": 3,
        "join_tolerance": 3,
        "edge_min_length": 3,
        "min_words_vertical": 1,
        "min_words_horizontal": 1,
    },
    {
        "vertical_strategy": "text",
        "horizontal_strategy": "text",
        "intersection_tolerance": 5,
        "snap_tolerance": 3,
        "join_tolerance": 3,
        "edge_min_length": 3,
        "min_words_vertical": 1,
        "min_words_horizontal": 1,
    },
)


class TimetablePDFValidationError(ValueError):
    pass


def _append_skipped_sample(skipped_rows: List[Dict], skipped_rows_omitted: int, payload: Dict) -> int:
    if len(skipped_rows) < SKIPPED_ROW_SAMPLE_CAP:
        skipped_rows.append(payload)
        return skipped_rows_omitted
    return skipped_rows_omitted + 1

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

    # Add DB-level duplicate protection. If legacy duplicates exist, continue with warnings.
    unique_index_sql = [
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_timetable_slots_dedupe ON timetable_slots (branch, section, semester, day, start_time, end_time, subject_name, faculty_name, room)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_timetable_entries_dedupe ON timetable_entries (branch_id, section, semester, day, start_time, end_time, subject_id, teacher_id, room)",
    ]
    for sql in unique_index_sql:
        try:
            _db_execute(db, sql)
        except Exception as e:
            logger.warning("Unique index creation skipped: %s", repr(e))
    try:
        db.commit()
    except Exception:
        pass


def _clean_text(value) -> str:
    return (str(value).strip() if value is not None else "")


def _dup_key(*parts) -> str:
    return "|".join(_clean_text(p) for p in parts)


def _insert_ignore_sql(db, table: str, columns: List[str]) -> str:
    column_list = ", ".join(columns)
    placeholder_list = ", ".join(["%s"] * len(columns))
    if _is_postgres_db(db):
        return f"INSERT INTO {table} ({column_list}) VALUES ({placeholder_list}) ON CONFLICT DO NOTHING"
    return f"INSERT OR IGNORE INTO {table} ({column_list}) VALUES ({placeholder_list})"


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

def iter_docx_table_slots(path: str) -> Iterator[Dict]:
    """Yield DOCX timetable slots row-by-row to keep memory usage low."""
    if docx is None:
        raise RuntimeError("python-docx is not installed. Install with: pip install python-docx")
    doc = docx.Document(path)
    parsed_rows = 0
    skipped_rows = 0
    failed_rows = 0
    skip_tokens = ("short break", "lunch break", "lib", "sports", "break")
    for table_index, table in enumerate(doc.tables):
        if not table.rows:
            continue
        headers = [_normalize_key(c.text) for c in table.rows[0].cells]
        for row_index, row in enumerate(table.rows[1:], start=1):
            values = [_clean_text(c.text) for c in row.cells]
            row_text = " | ".join(values)
            row_map = {headers[i]: values[i] for i in range(min(len(headers), len(values))) if headers[i]}
            try:
                normalized = _normalize_slot_row(row_map, row_text=row_text)
                subject_raw = normalized["subject_name"]
                faculty_raw = normalized["faculty_name"]
                if _row_has_token(subject_raw, *skip_tokens) or _row_has_token(faculty_raw, *skip_tokens) or _row_has_token(row_text, *skip_tokens):
                    skipped_rows += 1
                    continue
                if not _valid_slot_row(normalized):
                    skipped_rows += 1
                    continue
                subjects = _split_subjects(subject_raw)
                if not subjects:
                    skipped_rows += 1
                    continue
                for subject in subjects:
                    slot = dict(normalized)
                    slot["subject_name"] = _clean_text(subject)
                    slot["is_lab"] = int(bool(_row_has_token(subject, "lab", "practical") or normalized["is_lab"]))
                    if not _valid_slot_row(slot):
                        skipped_rows += 1
                        continue
                    parsed_rows += 1
                    yield slot
            except Exception:
                failed_rows += 1
                logger.exception("iter_docx_table_slots failed table=%s row=%s", table_index, row_index)
    logger.info("iter_docx_table_slots summary: tables=%d parsed_rows=%d skipped_rows=%d failed_rows=%d", len(doc.tables), parsed_rows, skipped_rows, failed_rows)


def iter_docx_grid_slots(path: str) -> Iterator[Dict]:
    """Yield grid-style DOCX slots row-by-row without retaining full table structures."""
    if docx is None:
        raise RuntimeError("python-docx is not installed. Install with: pip install python-docx")
    doc = docx.Document(path)
    base = os.path.splitext(os.path.basename(path))[0]
    title_text = " ".join([p.text for p in doc.paragraphs[:3] if p.text])
    inferred_branch = ""
    inferred_section = ""
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

    emitted = 0
    for table in doc.tables:
        if not table.rows:
            continue
        header_cells = table.rows[0].cells
        headers = [_clean_text(c.text) for c in header_cells]
        day_col = 0
        time_cols = []
        for i, h in enumerate(headers):
            low = h.lower()
            if "day" in low:
                day_col = i
            elif re.search(r"\d", h) or "-" in h:
                time_cols.append(i)
        if not time_cols:
            time_cols = [i for i in range(len(headers)) if i != day_col]

        for row in table.rows[1:]:
            day = _clean_text(row.cells[day_col].text)
            if not day:
                continue
            for col in time_cols:
                cell_text = _clean_text(row.cells[col].text) if col < len(row.cells) else ""
                if not cell_text:
                    continue
                head = _clean_text(header_cells[col].text) if col < len(header_cells) else ""
                start_time, end_time = _split_time_range(head)
                for subj in _split_subjects(cell_text):
                    s_text = subj
                    faculty = ""
                    if "\n" in cell_text:
                        lines = [l.strip() for l in cell_text.splitlines() if l.strip()]
                        if len(lines) >= 2:
                            s_text = _clean_text(lines[0])
                            faculty = _clean_text(lines[1])
                    else:
                        parts = re.split(r"[-/\\r\\n]+", subj)
                        if len(parts) >= 2:
                            s_text = _clean_text(parts[0])
                            faculty = _clean_text(parts[1])

                    slot = {
                        "branch": inferred_branch or base,
                        "section": inferred_section or "ALL",
                        "semester": None,
                        "day": day,
                        "start_time": start_time or "",
                        "end_time": end_time or "",
                        "subject_name": s_text,
                        "faculty_name": faculty,
                        "is_lab": int(bool(_row_has_token(s_text, "lab", "practical") or _row_has_token(cell_text, "lab", "practical"))),
                        "room": "",
                    }
                    if _valid_slot_row(slot):
                        emitted += 1
                        yield slot
    logger.info("iter_docx_grid_slots summary: tables=%d parsed_slots=%d", len(doc.tables), emitted)


def parse_docx_table(path: str) -> List[Dict]:
    """Compatibility wrapper for existing callers expecting a list."""
    return list(iter_docx_section_slots(path))


def parse_docx_grid(path: str) -> List[Dict]:
    """Compatibility wrapper for existing callers expecting a list."""
    return list(iter_docx_section_slots(path))


DOCX_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W_BODY = f"{{{DOCX_W_NS}}}body"
W_P = f"{{{DOCX_W_NS}}}p"
W_TBL = f"{{{DOCX_W_NS}}}tbl"
W_TR = f"{{{DOCX_W_NS}}}tr"
W_TC = f"{{{DOCX_W_NS}}}tc"
W_T = f"{{{DOCX_W_NS}}}t"
W_TC_PR = f"{{{DOCX_W_NS}}}tcPr"
W_GRID_SPAN = f"{{{DOCX_W_NS}}}gridSpan"
W_VMERGE = f"{{{DOCX_W_NS}}}vMerge"
W_VAL = f"{{{DOCX_W_NS}}}val"
W_BR = f"{{{DOCX_W_NS}}}br"
W_TAB = f"{{{DOCX_W_NS}}}tab"


def _docx_text(elem) -> str:
    parts = []
    for node in elem.iter():
        if node.tag == W_T and node.text:
            parts.append(node.text)
        elif node.tag in (W_BR, W_TAB):
            parts.append(" ")
    return _clean_text("".join(parts).replace("\xa0", " "))


def _docx_cell_span(tc) -> int:
    span = 1
    tc_pr = tc.find(W_TC_PR)
    if tc_pr is not None:
        grid_span = tc_pr.find(W_GRID_SPAN)
        if grid_span is not None:
            try:
                span = max(1, int(grid_span.attrib.get(W_VAL, "1") or 1))
            except Exception:
                span = 1
    return span


def _docx_cell_vmerge(tc) -> str:
    tc_pr = tc.find(W_TC_PR)
    if tc_pr is None:
        return ""
    vmerge = tc_pr.find(W_VMERGE)
    if vmerge is None:
        return ""
    return (vmerge.attrib.get(W_VAL) or "continue").strip().lower()


def _docx_expand_rows(table_elem) -> List[Dict]:
    rows = []
    previous_expanded = []
    for tr in table_elem.findall(W_TR):
        row_cells = []
        expanded = []
        logical_col = 0
        for tc in tr.findall(W_TC):
            text = _docx_text(tc)
            span = _docx_cell_span(tc)
            vmerge = _docx_cell_vmerge(tc)
            if vmerge == "continue" and logical_col < len(previous_expanded):
                text = previous_expanded[logical_col]
            row_cells.append(
                {
                    "text": text,
                    "span": span,
                    "start_col": logical_col,
                    "end_col": logical_col + span - 1,
                    "vmerge": vmerge,
                }
            )
            for _ in range(span):
                expanded.append(text)
                logical_col += 1
        if row_cells:
            rows.append({"cells": row_cells, "expanded": expanded})
            previous_expanded = expanded
    return rows


def _docx_iter_blocks(path: str):
    with zipfile.ZipFile(path) as archive:
        with archive.open("word/document.xml") as xml_fp:
            context = ET.iterparse(xml_fp, events=("start", "end"))
            stack = []
            for event, elem in context:
                if event == "start":
                    stack.append(elem)
                    continue
                parent = stack[-2] if len(stack) >= 2 else None
                if parent is not None and parent.tag == W_BODY:
                    if elem.tag == W_P:
                        yield ("paragraph", _docx_text(elem))
                        elem.clear()
                    elif elem.tag == W_TBL:
                        yield ("table", _docx_expand_rows(elem))
                        elem.clear()
                if stack:
                    stack.pop()


def scan_docx_structure(path: str, max_tables: Optional[int] = None) -> Dict[str, object]:
    section_names: List[str] = []
    table_count = 0
    faculty_tables = 0
    timetable_tables = 0
    for block_type, payload in _docx_iter_blocks(path):
        if block_type == "paragraph":
            text = _clean_text(payload)
            section_name = _section_from_text(text)
            if section_name and section_name not in section_names:
                section_names.append(section_name)
            continue
        if block_type != "table":
            continue
        table_count += 1
        if _is_faculty_table(payload):
            faculty_tables += 1
        if _is_timetable_table(payload):
            timetable_tables += 1
        if max_tables is not None and table_count >= max_tables:
            break
    return {
        "section_names": section_names,
        "table_count": table_count,
        "faculty_tables": faculty_tables,
        "timetable_tables": timetable_tables,
    }


def _section_from_text(text: str) -> str:
    text = _clean_text(text)
    if not text:
        return ""
    patterns = [
        r"\b([A-Z]{2,}[A-Z0-9]*(?:\s*[-/]\s*[A-Z0-9]{1,4})+)\b",
        r"\b([A-Z]{2,}[A-Z0-9]*\s*[-]\s*[A-Z0-9]{1,4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return re.sub(r"\s*[-/]\s*", "-", match.group(1).strip().upper())
    return ""


_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def _roman_to_int(value: str) -> Optional[int]:
    value = _clean_text(value).upper()
    if not value or any(ch not in _ROMAN_VALUES for ch in value):
        return None
    total = 0
    previous = 0
    for char in reversed(value):
        current = _ROMAN_VALUES[char]
        if current < previous:
            total -= current
        else:
            total += current
            previous = current
    return total or None


def _semester_from_text(text: str) -> Optional[int]:
    text = _clean_text(text)
    match = re.search(r"\b([IVX]+|\d+)\s*[-]?\s*Semester\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    value = match.group(1)
    if value.isdigit():
        try:
            return int(value)
        except Exception:
            return None
    return _roman_to_int(value)


def _subject_acronym(subject_name: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+", _clean_text(subject_name).upper())
    stopwords = {"AND", "OF", "THE", "FOR", "IN", "ON", "TO", "WITH", "A", "AN", "LAB", "PRACTICAL"}
    return "".join(token[0] for token in tokens if token and token not in stopwords)


def _faculty_lookup(entries: List[Dict]) -> Dict[str, Dict]:
    lookup: Dict[str, Dict] = {}
    for entry in entries:
        subject_name = _clean_text(entry.get("subject_name"))
        sub_code = _clean_text(entry.get("sub_code"))
        faculty_name = _clean_text(entry.get("faculty_name"))
        aliases = [
            _normalize_key(subject_name),
            _normalize_key(subject_name.replace("lab", "")),
            _normalize_key(sub_code),
            _normalize_key(_subject_acronym(subject_name)),
        ]
        for alias in aliases:
            if alias and alias not in lookup:
                lookup[alias] = {"subject_name": subject_name, "faculty_name": faculty_name, "sub_code": sub_code}
    return lookup


def _merge_faculty_entries(lookup: Dict[str, Dict], entries: List[Dict]) -> None:
    if not entries:
        return
    for entry in entries:
        subject_name = _clean_text(entry.get("subject_name"))
        sub_code = _clean_text(entry.get("sub_code"))
        faculty_name = _clean_text(entry.get("faculty_name"))
        aliases = [
            _normalize_key(subject_name),
            _normalize_key(subject_name.replace("lab", "")),
            _normalize_key(sub_code),
            _normalize_key(_subject_acronym(subject_name)),
        ]
        for alias in aliases:
            if alias and alias not in lookup:
                lookup[alias] = {
                    "subject_name": subject_name,
                    "faculty_name": faculty_name,
                    "sub_code": sub_code,
                }


def _resolve_subject(subject_text: str, lookup: Dict[str, Dict]) -> Dict:
    cleaned = _clean_text(subject_text)
    for candidate in (_normalize_key(cleaned), _normalize_key(cleaned.replace("lab", "")), _normalize_key(_subject_acronym(cleaned))):
        if candidate and candidate in lookup:
            return lookup[candidate]
    return {}


def _append_jsonl(path: str, payload: Dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        logger.exception("Failed to write jsonl event")


def _section_branch_name(section_name: str, fallback: str = "") -> str:
    section_name = _clean_text(section_name)
    if section_name and "-" in section_name:
        return _clean_text(section_name.split("-", 1)[0])
    return _clean_text(fallback)


def _is_faculty_table(table_rows: List[Dict]) -> bool:
    if not table_rows:
        return False
    header = _normalize_key(" ".join(table_rows[0].get("expanded") or []))
    return "sub code" in header and ("faculty" in header or "subject name" in header)


def _is_timetable_table(table_rows: List[Dict]) -> bool:
    if not table_rows:
        return False
    header = table_rows[0].get("expanded") or []
    header_text = " ".join(header)
    header_key = _normalize_key(header_text)
    if "day" in header_key and re.search(r"\d{1,2}:\d{2}", header_text):
        return True
    if any("break" in _normalize_key(value) for value in header):
        return True
    return False


def _expand_time_bounds(header_cells: List[str], start_col: int, end_col: int) -> tuple[str, str]:
    start_time = ""
    end_time = ""
    for header in header_cells[start_col:end_col + 1]:
        header_start, header_end = _split_time_range(header)
        if header_start and not start_time:
            start_time = header_start
        if header_end:
            end_time = header_end
    return start_time, end_time


def _finalize_section_slots(section_state: Dict) -> List[Dict]:
    faculty_map = section_state.get("faculty_map") or _faculty_lookup(section_state.get("faculty_entries", []))
    timetable_tables = section_state.get("timetable_tables", [])
    resolved_slots: List[Dict] = []
    for table_info in timetable_tables:
        table_rows = table_info.get("rows") or []
        for slot in _iter_section_table_slots(table_rows, section_state, faculty_map):
            resolved_slots.append(slot)
    return resolved_slots


def _iter_section_table_slots(table_rows: List[Dict], section_state: Dict, faculty_map: Dict[str, Dict]) -> Iterator[Dict]:
    if not table_rows:
        return
    section_name = _clean_text(section_state.get("section")) or _clean_text(section_state.get("section_hint"))
    branch_name = _clean_text(section_state.get("branch")) or _section_branch_name(section_name, section_state.get("doc_base", ""))
    semester = section_state.get("semester")
    header_cells = table_rows[0].get("expanded") or []
    day_col = 0
    for idx, header in enumerate(header_cells):
        if "day" in _normalize_key(header):
            day_col = idx
            break
    for row in table_rows[1:]:
        expanded = row.get("expanded") or []
        if not expanded:
            continue
        day = _clean_text(expanded[day_col]) if day_col < len(expanded) else ""
        if not day or _row_has_token(day, "short break", "lunch break", "break"):
            continue
        for cell in row.get("cells", []):
            if cell.get("start_col") == day_col:
                continue
            cell_text = _clean_text(cell.get("text"))
            if not cell_text or _row_has_token(cell_text, "short break", "lunch break", "break"):
                continue
            start_time, end_time = _expand_time_bounds(header_cells, cell.get("start_col", 0), cell.get("end_col", 0))
            if not start_time and not end_time:
                continue
            for subject_piece in _split_subjects(cell_text):
                resolved = _resolve_subject(subject_piece, faculty_map)
                subject_name = _clean_text(resolved.get("subject_name") or subject_piece)
                faculty_name = _clean_text(resolved.get("faculty_name") or "")
                slot = {
                    "branch": branch_name,
                    "section": section_name,
                    "semester": semester,
                    "day": day,
                    "start_time": start_time,
                    "end_time": end_time,
                    "subject_name": subject_name,
                    "faculty_name": faculty_name,
                    "is_lab": int(bool(_row_has_token(subject_piece, "lab", "practical") or _row_has_token(cell_text, "lab", "practical"))),
                    "room": _clean_text(section_state.get("room", "")),
                }
                if _valid_slot_row(slot):
                    yield slot


def iter_docx_section_slots(
    path: str,
    debug_jsonl_path: Optional[str] = None,
    single_section_only: bool = False,
    max_tables: Optional[int] = None,
) -> Iterator[Dict]:
    """Stream DOCX timetable rows section-by-section without materializing the full document."""
    if docx is None:
        raise RuntimeError("python-docx is not installed. Install with: pip install python-docx")

    base_name = os.path.splitext(os.path.basename(path))[0]
    section_state = {
        "section": "",
        "section_hint": "",
        "branch": "",
        "semester": None,
        "doc_base": base_name,
        "room": "",
        "faculty_map": {},
        "table_count": 0,
        "slot_count": 0,
    }
    primary_section = ""
    total_tables = 0
    total_slots = 0
    parse_failures = 0

    def flush_section(reason: str):
        nonlocal section_state, total_slots
        if section_state.get("table_count", 0) == 0 and not section_state.get("faculty_map"):
            return
        section_name = _clean_text(section_state.get("section")) or _clean_text(section_state.get("section_hint")) or f"section_{total_tables}"
        if debug_jsonl_path:
            _append_jsonl(
                debug_jsonl_path,
                {
                    "type": "section_flush",
                    "reason": reason,
                    "section": section_name,
                    "branch": _clean_text(section_state.get("branch")) or _section_branch_name(section_name, section_state.get("doc_base", "")),
                    "semester": section_state.get("semester"),
                    "table_count": section_state.get("table_count", 0),
                    "faculty_entry_count": len(section_state.get("faculty_map") or {}),
                    "slot_count": section_state.get("slot_count", 0),
                    "timestamp": time.time(),
                },
            )
        logger.info(
            "timetable section flush section=%s reason=%s tables=%d slots=%d",
            section_name,
            reason,
            section_state.get("table_count", 0),
            section_state.get("slot_count", 0),
        )
        section_state = {
            "section": "",
            "section_hint": "",
            "branch": "",
            "semester": None,
            "doc_base": base_name,
            "room": "",
            "faculty_map": {},
            "table_count": 0,
            "slot_count": 0,
        }
        gc.collect()

    try:
        for block_type, payload in _docx_iter_blocks(path):
            if block_type == "paragraph":
                text = _clean_text(payload)
                if not text:
                    continue
                section_name = _section_from_text(text)
                if section_name:
                    if single_section_only:
                        if not primary_section:
                            primary_section = section_name
                        elif section_name != primary_section:
                            raise ValueError(
                                f"Multiple sections detected ({primary_section}, {section_name}). Upload one section per DOCX."
                            )
                    if section_state.get("table_count", 0) > 0 or section_state.get("faculty_map"):
                        flush_section("new_section")
                    section_state["section"] = section_name
                    section_state["section_hint"] = section_name
                    section_state["branch"] = _section_branch_name(section_name, section_state.get("doc_base", ""))
                    if section_state["semester"] is None:
                        section_state["semester"] = _semester_from_text(text)
                    logger.info("timetable section start section=%s branch=%s semester=%s", section_name, section_state["branch"], section_state["semester"])
                    if debug_jsonl_path:
                        _append_jsonl(
                            debug_jsonl_path,
                            {
                                "type": "section_start",
                                "section": section_name,
                                "branch": section_state["branch"],
                                "semester": section_state["semester"],
                                "timestamp": time.time(),
                            },
                        )
                    continue
                semester = _semester_from_text(text)
                if semester is not None and section_state["semester"] is None:
                    section_state["semester"] = semester
                if not section_state["branch"]:
                    section_state["branch"] = _section_branch_name(text, section_state.get("doc_base", ""))
                continue

            if block_type != "table":
                continue

            table_rows = payload or []
            total_tables += 1
            if max_tables is not None and total_tables > max_tables:
                raise ValueError(
                    f"Too many tables detected ({total_tables}). Split the timetable into section-wise DOCX files."
                )
            try:
                if _is_faculty_table(table_rows):
                    entries = []
                    for row in table_rows[1:]:
                        expanded = row.get("expanded") or []
                        for offset in range(0, len(expanded), 3):
                            group = expanded[offset:offset + 3]
                            if len(group) < 3:
                                continue
                            sub_code = _clean_text(group[0])
                            subject_name = _clean_text(group[1])
                            faculty_name = _clean_text(group[2])
                            if not (sub_code or subject_name or faculty_name):
                                continue
                            if _normalize_key(sub_code) in {"sub code", "subject code"} or _normalize_key(subject_name) == "subject name":
                                continue
                            entries.append({"sub_code": sub_code, "subject_name": subject_name, "faculty_name": faculty_name})
                    _merge_faculty_entries(section_state["faculty_map"], entries)
                    section_state["table_count"] += 1
                    if debug_jsonl_path:
                        _append_jsonl(debug_jsonl_path, {"type": "faculty_table", "section": section_state.get("section") or section_state.get("section_hint") or f"section_{total_tables}", "entries": len(entries), "timestamp": time.time()})
                    entries = None
                    table_rows = None
                    gc.collect()
                    continue

                if _is_timetable_table(table_rows) or section_state.get("table_count", 0) == 0:
                    section_state["table_count"] += 1
                    for slot in _iter_section_table_slots(table_rows, section_state, section_state.get("faculty_map") or {}):
                        section_state["slot_count"] += 1
                        total_slots += 1
                        yield slot
                    mem_estimate_kb = (len(table_rows) * 180 + len(section_state.get("faculty_map") or {}) * 80) / 1024.0
                    logger.info(
                        "timetable table processed section=%s rows=%d slots=%d tables=%d mem_estimate_kb=%.1f",
                        section_state.get("section") or section_state.get("section_hint") or f"section_{total_tables}",
                        len(table_rows),
                        section_state.get("slot_count", 0),
                        section_state.get("table_count", 0),
                        mem_estimate_kb,
                    )
                    if debug_jsonl_path:
                        _append_jsonl(debug_jsonl_path, {"type": "timetable_table", "section": section_state.get("section") or section_state.get("section_hint") or f"section_{total_tables}", "rows": len(table_rows), "timestamp": time.time()})
                table_rows = None
                gc.collect()
            except Exception:
                parse_failures += 1
                logger.exception("Failed to classify DOCX table index=%d", total_tables)
                continue

    except Exception:
        logger.exception("DOCX section iterator failed for %s", path)
        raise

    flush_section("eof")
    logger.info("iter_docx_section_slots summary: tables=%d slots=%d failures=%d file=%s", total_tables, total_slots, parse_failures, os.path.basename(path))


_PDF_DAY_ALIASES = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}

_PDF_DAY_RE = re.compile(r"\b(mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b", re.IGNORECASE)
_PDF_TIME_RANGE_RE = re.compile(r"(\d{1,2}[:\.]\d{2}\s*(?:AM|PM)?)\s*(?:-|to)\s*(\d{1,2}[:\.]\d{2}\s*(?:AM|PM)?)", re.IGNORECASE)
_PDF_TIME_RANGE_HOUR_RE = re.compile(r"(\d{1,2}\s*(?:AM|PM))\s*(?:-|to)\s*(\d{1,2}\s*(?:AM|PM))", re.IGNORECASE)
_PDF_DECORATIVE_TOKENS = ("principal", "hod", "head of department", "department", "dean")
_PDF_BREAK_TOKENS = ("short break", "lunch break", "break", "lunch")


def _normalize_day_name(token: str) -> str:
    key = _clean_text(token).lower()[:3]
    return _PDF_DAY_ALIASES.get(key, token.title())


def _extract_pdf_day(text: str) -> tuple[str, str]:
    match = _PDF_DAY_RE.search(text or "")
    if not match:
        return "", ""
    return _normalize_day_name(match.group(1)), match.group(0)


def _extract_pdf_time_range(text: str) -> tuple[str, str, str]:
    match = _PDF_TIME_RANGE_RE.search(text or "") or _PDF_TIME_RANGE_HOUR_RE.search(text or "")
    if not match:
        return "", "", ""
    start = _format_time_str(match.group(1)) or ""
    end = _format_time_str(match.group(2)) or ""
    return start, end, match.group(0)


def _pdf_text_has_time(text: str) -> bool:
    return bool(_PDF_TIME_RANGE_RE.search(text or "") or _PDF_TIME_RANGE_HOUR_RE.search(text or ""))


def _pdf_header_has_time(text: str) -> bool:
    return bool(re.search(r"\d{1,2}[:\.]\d{2}", text or "") or re.search(r"\b\d{1,2}\s*(AM|PM)\b", text or "", flags=re.IGNORECASE))


def _pdf_is_break(text: str) -> bool:
    return _row_has_token(text, *_PDF_BREAK_TOKENS)


def _pdf_is_decorative_line(text: str) -> bool:
    return _row_has_token(text, *_PDF_DECORATIVE_TOKENS)


def _pdf_collect_section_candidates(text: str) -> List[str]:
    sections: List[str] = []
    for line in (text or "").splitlines():
        cleaned = _clean_text(line)
        if not cleaned or _pdf_is_decorative_line(cleaned):
            continue
        section = _section_from_text(cleaned)
        if section and section not in sections:
            sections.append(section)
    return sections


def _pdf_bbox_key(bbox) -> tuple:
    if not bbox or len(bbox) < 4:
        return ()
    return (int(round(bbox[0])), int(round(bbox[1])), int(round(bbox[2])), int(round(bbox[3])))


def _pdf_find_tables(page) -> List[Dict]:
    tables: List[Dict] = []
    seen = set()
    for settings in PDF_TABLE_SETTINGS:
        try:
            found = page.find_tables(table_settings=settings) or []
        except Exception:
            found = []
        for table in found:
            bbox = getattr(table, "bbox", None)
            key = _pdf_bbox_key(bbox)
            if not key or key in seen:
                continue
            seen.add(key)
            tables.append({"table": table, "bbox": bbox, "key": key, "settings": settings})
    tables.sort(key=lambda t: (t["bbox"][1], t["bbox"][0]) if t.get("bbox") else (0, 0))
    return tables


def _pdf_table_extract_matrix(table) -> List[List[str]]:
    try:
        raw = table.extract() or []
    except Exception:
        raw = []
    if not raw:
        return []
    max_cols = max((len(r) for r in raw if r), default=0)
    rows = []
    for row in raw:
        row = row or []
        cleaned = [_clean_text(c) for c in row]
        if max_cols and len(cleaned) < max_cols:
            cleaned.extend([""] * (max_cols - len(cleaned)))
        rows.append(cleaned)
    return rows


def _pdf_locate_timetable_header(rows: List[List[str]]) -> tuple[int, List[str]]:
    for idx in range(min(3, len(rows))):
        row = rows[idx] or []
        if not row:
            continue
        has_day_header = any("day" in _normalize_key(c) for c in row if c)
        time_hits = sum(1 for c in row if _pdf_header_has_time(c))
        if time_hits >= 2 and (has_day_header or any(_PDF_DAY_RE.search(c or "") for c in row)):
            return idx, row
    return -1, []


def _pdf_locate_faculty_header(rows: List[List[str]]) -> tuple[int, List[str]]:
    for idx in range(min(3, len(rows))):
        row = rows[idx] or []
        header_key = _normalize_key(" ".join(row))
        if "faculty" in header_key and ("sub code" in header_key or "subject code" in header_key or "subject name" in header_key):
            return idx, row
    return -1, []


def _pdf_is_faculty_table_rows(rows: List[List[str]]) -> bool:
    header_idx, header_cells = _pdf_locate_faculty_header(rows)
    if header_idx < 0:
        return False
    header_key = _normalize_key(" ".join(header_cells))
    return ("sub code" in header_key or "subject code" in header_key) and "faculty" in header_key


def _pdf_parse_faculty_rows(rows: List[List[str]]) -> List[Dict]:
    header_idx, header_cells = _pdf_locate_faculty_header(rows)
    if header_idx < 0:
        return []
    header_key = [_normalize_key(c) for c in header_cells]
    code_idx = None
    name_idx = None
    faculty_idx = None
    for idx, key in enumerate(header_key):
        if code_idx is None and ("sub code" in key or "subject code" in key):
            code_idx = idx
        elif name_idx is None and "subject name" in key:
            name_idx = idx
        elif faculty_idx is None and "faculty" in key:
            faculty_idx = idx
    entries: List[Dict] = []
    for row in rows[header_idx + 1:]:
        if not row or not any(_clean_text(c) for c in row):
            continue
        sub_code = _clean_text(row[code_idx]) if code_idx is not None and code_idx < len(row) else ""
        subject_name = _clean_text(row[name_idx]) if name_idx is not None and name_idx < len(row) else ""
        faculty_name = _clean_text(row[faculty_idx]) if faculty_idx is not None and faculty_idx < len(row) else ""
        if not (sub_code or subject_name or faculty_name):
            continue
        if _normalize_key(sub_code) in {"sub code", "subject code"}:
            continue
        entries.append({"sub_code": sub_code, "subject_name": subject_name, "faculty_name": faculty_name})
    return entries


def _pdf_cell_bbox(cell) -> Optional[tuple]:
    if cell is None:
        return None
    if isinstance(cell, dict):
        if all(k in cell for k in ("x0", "top", "x1", "bottom")):
            return (cell["x0"], cell["top"], cell["x1"], cell["bottom"])
        if "bbox" in cell:
            return cell.get("bbox")
    if hasattr(cell, "bbox"):
        return getattr(cell, "bbox")
    return None


def _pdf_cell_text(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, dict):
        return _clean_text(cell.get("text", ""))
    if hasattr(cell, "text"):
        return _clean_text(getattr(cell, "text") or "")
    return ""


def _pdf_table_column_bounds(table, col_count: int) -> List[tuple]:
    bounds = []
    try:
        cols = getattr(table, "columns", None)
        if cols:
            for col in cols:
                bbox = _pdf_cell_bbox(col)
                if bbox and len(bbox) >= 3:
                    bounds.append((bbox[0], bbox[2]))
    except Exception:
        bounds = []
    if bounds and len(bounds) >= col_count:
        return bounds
    return bounds


def _pdf_col_span_for_bbox(bbox: tuple, col_bounds: List[tuple]) -> tuple[Optional[int], Optional[int]]:
    x0 = bbox[0]
    x1 = bbox[2]
    indices = [i for i, (c0, c1) in enumerate(col_bounds) if c1 > x0 and c0 < x1]
    if not indices:
        return None, None
    return min(indices), max(indices)


def _pdf_table_row_spans(table, row_index: int, col_bounds: List[tuple]) -> Optional[List[Dict]]:
    if not table or not col_bounds:
        return None
    try:
        rows = getattr(table, "rows", None)
        if not rows or row_index >= len(rows):
            return None
        row = rows[row_index]
        cells = getattr(row, "cells", None)
        if not cells:
            return None
        spans = []
        for cell in cells:
            text = _pdf_cell_text(cell)
            if not text:
                continue
            bbox = _pdf_cell_bbox(cell)
            if not bbox:
                continue
            start_col, end_col = _pdf_col_span_for_bbox(bbox, col_bounds)
            if start_col is None or end_col is None:
                continue
            spans.append({"start_col": start_col, "end_col": end_col, "text": text})
        if spans:
            spans.sort(key=lambda s: (s["start_col"], s["end_col"]))
            return spans
    except Exception:
        return None
    return None


def _pdf_row_spans_from_values(row_values: List[str], header_slots: List[Dict], day_col: int) -> List[Dict]:
    spans: List[Dict] = []
    current_text = ""
    current_start = None
    for idx, cell in enumerate(row_values):
        if idx == day_col:
            continue
        if idx < len(header_slots) and header_slots[idx].get("is_break"):
            if current_start is not None and current_text:
                spans.append({"start_col": current_start, "end_col": idx - 1, "text": current_text})
            current_text = ""
            current_start = None
            continue
        text = _clean_text(cell)
        if text:
            if current_start is None:
                current_text = text
                current_start = idx
            else:
                if text == current_text and _row_has_token(text, "lab", "practical"):
                    continue
                spans.append({"start_col": current_start, "end_col": idx - 1, "text": current_text})
                current_text = text
                current_start = idx
        else:
            if current_start is not None and current_text and _row_has_token(current_text, "lab", "practical"):
                continue
            if current_start is not None and current_text:
                spans.append({"start_col": current_start, "end_col": idx - 1, "text": current_text})
            current_text = ""
            current_start = None
    if current_start is not None and current_text:
        spans.append({"start_col": current_start, "end_col": len(row_values) - 1, "text": current_text})
    return spans


def _pdf_split_span_on_breaks(span: Dict, header_slots: List[Dict], day_col: int) -> List[tuple]:
    segments = []
    current = None
    for col in range(span["start_col"], span["end_col"] + 1):
        if col == day_col:
            continue
        if col < len(header_slots) and header_slots[col].get("is_break"):
            if current:
                segments.append((current[0], current[1]))
            current = None
            continue
        if current is None:
            current = [col, col]
        else:
            current[1] = col
    if current:
        segments.append((current[0], current[1]))
    return segments


def _pdf_find_day_col(header_cells: List[str], rows: List[List[str]], header_idx: int) -> Optional[int]:
    for idx, cell in enumerate(header_cells):
        if "day" in _normalize_key(cell):
            return idx
    day_counts = {}
    for row in rows[header_idx + 1:]:
        for idx, cell in enumerate(row):
            day, _ = _extract_pdf_day(cell)
            if day:
                day_counts[idx] = day_counts.get(idx, 0) + 1
    if not day_counts:
        return None
    return max(day_counts.items(), key=lambda kv: kv[1])[0]


def _pdf_build_header_slots(header_cells: List[str]) -> List[Dict]:
    slots = []
    for idx, text in enumerate(header_cells):
        start, end, _ = _extract_pdf_time_range(text)
        slots.append({
            "index": idx,
            "label": _clean_text(text),
            "start_time": start,
            "end_time": end,
            "is_break": _pdf_is_break(text),
        })
    return slots


def _pdf_parse_timetable_table(
    table_info: Dict,
    section_name: str,
    branch_name: str,
    semester: Optional[int],
    faculty_map: Dict[str, Dict],
    report: Dict[str, object],
) -> Iterator[Dict]:
    rows = table_info.get("rows") or []
    header_idx = table_info.get("header_idx", -1)
    header_cells = table_info.get("header_cells") or []
    if header_idx < 0 or not header_cells:
        report["validation_errors"].append("Timetable header row not detected (Day/time headers missing).")
        raise TimetablePDFValidationError("Timetable header row not detected. Ensure the grid has a Day column and time slot headers.")

    day_col = _pdf_find_day_col(header_cells, rows, header_idx)
    if day_col is None:
        report["validation_errors"].append("Day column not detected in the timetable grid.")
        raise TimetablePDFValidationError("Day column not detected in the timetable grid. Ensure the first column is labeled Day and lists MON/TUE/WED...")

    header_slots = _pdf_build_header_slots(header_cells)
    time_slots = [slot for slot in header_slots if slot["index"] != day_col and not slot.get("is_break") and (slot.get("start_time") or slot.get("end_time") or slot.get("label"))]
    if not any(slot.get("start_time") or slot.get("end_time") for slot in time_slots):
        report["validation_errors"].append("Time slot headers not detected in the timetable grid.")
        raise TimetablePDFValidationError("Time slot headers not detected. Ensure the header row contains time ranges like 09:00-10:00.")

    detected_days = set()
    detected_subjects = report.get("extracted_subjects_sample") or []
    detected_time_slots = report.get("detected_time_slots") or []
    if not detected_time_slots:
        for slot in time_slots:
            start = slot.get("start_time") or ""
            end = slot.get("end_time") or ""
            label = f"{start}-{end}" if start and end else (slot.get("label") or "")
            if label and label not in detected_time_slots:
                detected_time_slots.append(label)
    report["detected_time_slots"] = detected_time_slots[:PDF_DIAG_SAMPLE_CAP]

    col_bounds = _pdf_table_column_bounds(table_info.get("table"), len(header_cells))
    for row_index, row in enumerate(rows[header_idx + 1:], start=header_idx + 1):
        if not row:
            continue
        if len(row) < len(header_cells):
            row = row + [""] * (len(header_cells) - len(row))
        day_raw = _clean_text(row[day_col]) if day_col < len(row) else ""
        day, _ = _extract_pdf_day(day_raw)
        if not day:
            continue
        detected_days.add(day)

        spans = _pdf_table_row_spans(table_info.get("table"), row_index, col_bounds)
        if spans is None:
            spans = _pdf_row_spans_from_values(row, header_slots, day_col)

        for span in spans:
            text = _clean_text(span.get("text"))
            if not text or _pdf_is_break(text):
                continue
            for seg_start, seg_end in _pdf_split_span_on_breaks(span, header_slots, day_col):
                start_time, end_time = _expand_time_bounds(header_cells, seg_start, seg_end)
                if not start_time:
                    start_time = header_slots[seg_start].get("start_time") if seg_start < len(header_slots) else ""
                if not end_time:
                    end_time = header_slots[seg_end].get("end_time") if seg_end < len(header_slots) else ""
                if not start_time and not end_time:
                    continue
                for subject_piece in _split_subjects(text):
                    resolved = _resolve_subject(subject_piece, faculty_map)
                    subject_name = _clean_text(resolved.get("subject_name") or subject_piece)
                    faculty_name = _clean_text(resolved.get("faculty_name") or "")
                    slot = {
                        "branch": branch_name,
                        "section": section_name,
                        "semester": semester,
                        "day": day,
                        "start_time": start_time,
                        "end_time": end_time,
                        "subject_name": subject_name,
                        "faculty_name": faculty_name,
                        "is_lab": int(bool(_row_has_token(subject_piece, "lab", "practical") or _row_has_token(text, "lab", "practical"))),
                        "room": "",
                    }
                    if subject_name and subject_name not in detected_subjects:
                        detected_subjects.append(subject_name)
                    yield slot

    report["detected_days"] = sorted(detected_days)
    report["extracted_subjects_sample"] = detected_subjects[:PDF_DIAG_SAMPLE_CAP]


def _pdf_score_timetable_table(rows: List[List[str]], header_idx: int) -> int:
    if header_idx < 0 or header_idx >= len(rows):
        return 0
    header_cells = rows[header_idx] or []
    time_hits = sum(1 for c in header_cells if _pdf_header_has_time(c))
    return time_hits * 10 + len(rows)


def parse_pdf_to_slots(path: str, stats: Optional[Dict[str, object]] = None) -> Iterator[Dict]:
    if pdfplumber is None:
        raise RuntimeError("pdfplumber is not installed. Install with: pip install pdfplumber")
    report = stats if stats is not None else {}
    report.setdefault("tables_detected", 0)
    report.setdefault("timetable_tables", 0)
    report.setdefault("faculty_tables", 0)
    report.setdefault("detected_section", "")
    report.setdefault("detected_days", [])
    report.setdefault("detected_time_slots", [])
    report.setdefault("extracted_subjects_sample", [])
    report.setdefault("faculty_mappings_sample", [])
    report.setdefault("faculty_mappings_count", 0)
    report.setdefault("validation_errors", [])

    base_name = os.path.splitext(os.path.basename(path))[0]
    section_candidates = []
    base_section = _section_from_text(base_name)
    if base_section:
        section_candidates.append(base_section)
    semester_hint = None

    timetable_tables = []
    faculty_tables = []

    with pdfplumber.open(path) as pdf:
        for page_index, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            section_candidates.extend(_pdf_collect_section_candidates(page_text))
            if semester_hint is None:
                semester_hint = _semester_from_text(page_text)

            tables = _pdf_find_tables(page)
            report["tables_detected"] += len(tables)
            for info in tables:
                rows = _pdf_table_extract_matrix(info["table"])
                if not rows:
                    continue
                info["rows"] = rows
                header_idx, header_cells = _pdf_locate_timetable_header(rows)
                info["header_idx"] = header_idx
                info["header_cells"] = header_cells
                if _pdf_is_faculty_table_rows(rows):
                    faculty_tables.append(info)
                    continue
                if header_idx >= 0:
                    timetable_tables.append(info)
                    continue

    unique_sections = []
    for section in section_candidates:
        if section and section not in unique_sections:
            unique_sections.append(section)

    if TIMETABLE_SINGLE_SECTION_ONLY and len(unique_sections) > 1:
        preview = ", ".join(unique_sections[:4])
        suffix = "..." if len(unique_sections) > 4 else ""
        report["validation_errors"].append(
            f"Multiple sections detected ({preview}{suffix})."
        )
        raise TimetablePDFValidationError(
            f"Multiple sections detected in PDF ({preview}{suffix}). Please upload one section per PDF."
        )

    section_name = unique_sections[0] if unique_sections else (base_section or base_name)
    branch_name = _section_branch_name(section_name, base_name)
    report["detected_section"] = section_name

    if not timetable_tables:
        report["validation_errors"].append("Timetable grid not detected (expected Day column and time slot headers).")
        raise TimetablePDFValidationError("Timetable grid not detected. Ensure the PDF has a timetable table with Day and time-slot headers.")
    if not faculty_tables:
        report["validation_errors"].append("Faculty mapping table not detected (expected Subject Code/Name/Faculty columns).")
        raise TimetablePDFValidationError("Faculty mapping table not detected. Ensure the PDF includes the Subject Code / Subject Name / Faculty Name table below the grid.")

    report["timetable_tables"] = len(timetable_tables)
    report["faculty_tables"] = len(faculty_tables)

    faculty_entries: List[Dict] = []
    for table_info in faculty_tables:
        entries = _pdf_parse_faculty_rows(table_info.get("rows") or [])
        faculty_entries.extend(entries)
    report["faculty_mappings_count"] = len(faculty_entries)
    if faculty_tables and not faculty_entries:
        report["validation_errors"].append("Faculty table detected but no rows parsed (check Subject Code/Name/Faculty columns).")
        raise TimetablePDFValidationError("Faculty mapping table detected but rows could not be parsed. Ensure the columns are Subject Code, Subject Name, and Faculty Name.")
    if faculty_entries:
        report["faculty_mappings_sample"] = faculty_entries[:PDF_DIAG_SAMPLE_CAP]

    faculty_map = _faculty_lookup(faculty_entries)
    best_table = max(
        timetable_tables,
        key=lambda t: _pdf_score_timetable_table(t.get("rows") or [], t.get("header_idx", -1)),
    )

    for slot in _pdf_parse_timetable_table(best_table, section_name, branch_name, semester_hint, faculty_map, report):
        yield slot


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


def _resolve_or_create_branch_id(db, branch_name: str, branch_cache: Optional[Dict[str, Optional[int]]] = None):
    branch_name = _clean_text(branch_name)
    if not branch_name:
        return None

    cache_key = branch_name.lower()
    if branch_cache is not None and cache_key in branch_cache:
        return branch_cache[cache_key]

    candidates = [branch_name]
    if "-" in branch_name:
        candidates.append(_clean_text(branch_name.split("-", 1)[0]))
    if branch_name.endswith("SECTION"):
        candidates.append(branch_name[:-7].strip())

    for candidate in candidates:
        if not candidate:
            continue
        branch_id = _lookup_branch_id(db, candidate)
        if branch_id is not None:
            if branch_cache is not None:
                branch_cache[cache_key] = branch_id
                branch_cache[candidate.lower()] = branch_id
            return branch_id

    try:
        _db_execute(db, _insert_ignore_sql(db, "branches", ["name", "location"]), (branch_name, "Auto-imported timetable branch"))
        try:
            db.commit()
        except Exception:
            pass
    except Exception:
        logger.exception("Failed to auto-create branch %s", branch_name)
        return None

    branch_id = _lookup_branch_id(db, branch_name)
    if branch_cache is not None:
        branch_cache[cache_key] = branch_id
    return branch_id


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

def _write_preview_line(preview_path: str, preview_state: Dict[str, int], payload: Dict):
    if not preview_path or PREVIEW_ROW_CAP <= 0:
        return
    if preview_state["written"] >= PREVIEW_ROW_CAP:
        return
    try:
        with open(preview_path, "a", encoding="utf-8") as pf:
            pf.write(json.dumps(payload, default=str) + "\n")
        preview_state["written"] += 1
    except Exception:
        logger.exception("Failed to write import preview line")


def _iter_docx_slots_with_fallback(path: str) -> Iterator[Dict]:
    yield from iter_docx_section_slots(path)


def import_slots_streaming(db, slots_iter: Iterable[Dict]):
    """Import slots from an iterator to avoid retaining full parsed timetable in memory."""
    raw_counters = {
        "processed": 0,
        "inserted": 0,
        "skipped_total": 0,
        "skipped_invalid": 0,
        "skipped_duplicate": 0,
        "failures": 0,
    }
    normalized_counters = {
        "processed": 0,
        "inserted": 0,
        "skipped_total": 0,
        "skipped_branch": 0,
        "skipped_duplicate": 0,
        "missing_subjects": 0,
        "missing_teachers": 0,
        "failures": 0,
    }

    preview_path = None
    preview_state = {"written": 0}
    if PREVIEW_ROW_CAP > 0:
        preview_path = os.path.join(os.path.dirname(__file__), "uploads", "last_import_debug.jsonl")
        os.makedirs(os.path.dirname(preview_path), exist_ok=True)
    # Commit every 5-10 rows for low-memory environments
    batch_size = max(5, min(10, int(BATCH_INSERT_SIZE)))
    inserted_since_commit = 0
    batch_commits = 0
    start_ts = time.time()
    peak_mem_estimate_bytes = 0

    branch_cache = {}
    subject_cache = {}
    teacher_cache = {}
    for row_index, slot_in in enumerate(slots_iter, start=1):
        mem_estimate = (
            (len(branch_cache) + len(subject_cache) + len(teacher_cache)) * 80
            + preview_state["written"] * 64
        )
        if mem_estimate > peak_mem_estimate_bytes:
            peak_mem_estimate_bytes = mem_estimate

        row = {
            "branch": _clean_text(slot_in.get("branch")),
            "section": _clean_text(slot_in.get("section")),
            "semester": _safe_int(slot_in.get("semester")),
            "day": _clean_text(slot_in.get("day")),
            "start_time": _clean_text(slot_in.get("start_time")),
            "end_time": _clean_text(slot_in.get("end_time")),
            "subject_name": _clean_text(slot_in.get("subject_name")),
            "faculty_name": _clean_text(slot_in.get("faculty_name")),
            "is_lab": int(bool(slot_in.get("is_lab"))),
            "room": _clean_text(slot_in.get("room")),
        }
        raw_counters["processed"] += 1

        if not _valid_slot_row(row):
            raw_counters["skipped_total"] += 1
            raw_counters["skipped_invalid"] += 1
            _write_preview_line(preview_path, preview_state, {"index": row_index, "reason": "invalid_row", "row": row})
            continue

        try:
            cur = _db_execute(
                db,
                _insert_ignore_sql(db, "timetable_slots", ["branch", "section", "semester", "day", "start_time", "end_time", "subject_name", "faculty_name", "is_lab", "room"]),
                (row["branch"], row["section"], row["semester"], row["day"], row["start_time"], row["end_time"], row["subject_name"], row["faculty_name"], row["is_lab"], row["room"]),
            )
            if hasattr(cur, "rowcount") and int(cur.rowcount or 0) == 0:
                raw_counters["skipped_total"] += 1
                raw_counters["skipped_duplicate"] += 1
            else:
                raw_counters["inserted"] += 1
                inserted_since_commit += 1
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raw_counters["skipped_total"] += 1
                raw_counters["skipped_duplicate"] += 1
                continue
            raw_counters["failures"] += 1
            _write_preview_line(preview_path, preview_state, {"index": row_index, "reason": "raw_insert_exception", "row": row})
            raise

        normalized_counters["processed"] += 1
        branch_key = row["branch"].strip().lower()
        if branch_key not in branch_cache:
            branch_cache[branch_key] = _resolve_or_create_branch_id(db, row["branch"], branch_cache)
        branch_id = branch_cache.get(branch_key)
        if branch_id is None:
            normalized_counters["skipped_total"] += 1
            normalized_counters["skipped_branch"] += 1
            _write_preview_line(preview_path, preview_state, {"index": row_index, "reason": "missing_branch", "row": row})
        else:
            subj_key = row["subject_name"].strip().lower()
            if subj_key not in subject_cache:
                s_row = _db_execute(db, "SELECT id FROM subjects WHERE LOWER(TRIM(name))=LOWER(TRIM(%s)) LIMIT 1", (row["subject_name"],)).fetchone()
                subject_cache[subj_key] = s_row[0] if s_row and not hasattr(s_row, "keys") else (s_row["id"] if s_row else None)
            subject_id = subject_cache.get(subj_key)
            if subject_id is None:
                normalized_counters["missing_subjects"] += 1

            teacher_id = None
            if row["faculty_name"]:
                t_key = row["faculty_name"].strip().lower()
                if t_key not in teacher_cache:
                    t_row = _db_execute(db, "SELECT id FROM teachers WHERE LOWER(TRIM(name))=LOWER(TRIM(%s)) LIMIT 1", (row["faculty_name"],)).fetchone()
                    teacher_cache[t_key] = t_row[0] if t_row and not hasattr(t_row, "keys") else (t_row["id"] if t_row else None)
                teacher_id = teacher_cache.get(t_key)
                if teacher_id is None:
                    normalized_counters["missing_teachers"] += 1

            norm_row = {
                "branch_id": branch_id,
                "section": row["section"],
                "semester": row["semester"],
                "day": row["day"],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "subject_id": subject_id,
                "teacher_id": teacher_id,
                "is_lab": row["is_lab"],
                "room": row["room"],
            }
            if norm_row["subject_id"] is None or norm_row["teacher_id"] is None:
                if _row_exists(
                    db,
                    "timetable_entries",
                    "COALESCE(CAST(branch_id AS TEXT), '') = COALESCE(CAST(%s AS TEXT), '') AND COALESCE(section, '') = COALESCE(%s, '') AND COALESCE(CAST(semester AS TEXT), '') = COALESCE(CAST(%s AS TEXT), '') AND COALESCE(day, '') = COALESCE(%s, '') AND COALESCE(start_time, '') = COALESCE(%s, '') AND COALESCE(end_time, '') = COALESCE(%s, '') AND COALESCE(CAST(subject_id AS TEXT), '') = COALESCE(CAST(%s AS TEXT), '') AND COALESCE(CAST(teacher_id AS TEXT), '') = COALESCE(CAST(%s AS TEXT), '') AND COALESCE(room, '') = COALESCE(%s, '')",
                    (norm_row["branch_id"], norm_row["section"], norm_row["semester"], norm_row["day"], norm_row["start_time"], norm_row["end_time"], norm_row["subject_id"], norm_row["teacher_id"], norm_row["room"]),
                ):
                    normalized_counters["skipped_total"] += 1
                    normalized_counters["skipped_duplicate"] += 1
                    continue
            try:
                cur = _db_execute(
                    db,
                    _insert_ignore_sql(db, "timetable_entries", ["branch_id", "section", "semester", "day", "start_time", "end_time", "subject_id", "teacher_id", "is_lab", "room"]),
                    (norm_row["branch_id"], norm_row["section"], norm_row["semester"], norm_row["day"], norm_row["start_time"], norm_row["end_time"], norm_row["subject_id"], norm_row["teacher_id"], norm_row["is_lab"], norm_row["room"]),
                )
                if hasattr(cur, "rowcount") and int(cur.rowcount or 0) == 0:
                    normalized_counters["skipped_total"] += 1
                    normalized_counters["skipped_duplicate"] += 1
                else:
                    normalized_counters["inserted"] += 1
                    inserted_since_commit += 1
            except Exception as e:
                if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                    normalized_counters["skipped_total"] += 1
                    normalized_counters["skipped_duplicate"] += 1
                else:
                    normalized_counters["failures"] += 1
                    _write_preview_line(preview_path, preview_state, {"index": row_index, "reason": "normalized_insert_exception", "row": norm_row})
                    raise

        if inserted_since_commit >= batch_size:
            db.commit()
            batch_commits += 1
            inserted_since_commit = 0
            if len(branch_cache) > 64 or len(subject_cache) > 64 or len(teacher_cache) > 64:
                logger.debug("Clearing timetable import caches (branch=%d, subject=%d, teacher=%d)", len(branch_cache), len(subject_cache), len(teacher_cache))
                branch_cache.clear()
                subject_cache.clear()
                teacher_cache.clear()
            logger.info(
                "import batch commit section=%s processed=%d commits=%d mem_estimate_kb=%.1f",
                row.get("section"),
                row_index,
                batch_commits,
                mem_estimate / 1024.0,
            )
            _write_preview_line(
                preview_path,
                preview_state,
                {"type": "batch_commit", "processed": row_index, "raw_inserted": raw_counters["inserted"], "normalized_inserted": normalized_counters["inserted"], "timestamp": time.time()},
            )

        if row_index % max(10, batch_size) == 0:
            elapsed = time.time() - start_ts
            rate = row_index / elapsed if elapsed > 0 else 0
            logger.info(
                "import_slots_streaming progress section=%s processed=%d raw_inserted=%d normalized_inserted=%d skipped=%d commits=%d elapsed=%.1fs rate=%.1f rows/s peak_mem_estimate_kb=%.1f",
                row["section"],
                row_index,
                raw_counters["inserted"],
                normalized_counters["inserted"],
                raw_counters["skipped_total"] + normalized_counters["skipped_total"],
                batch_commits,
                elapsed,
                rate,
                peak_mem_estimate_bytes / 1024.0,
            )

    if inserted_since_commit > 0:
        db.commit()
        batch_commits += 1

    # Final cleanup to free memory
    branch_cache.clear()
    subject_cache.clear()
    teacher_cache.clear()
    gc.collect()

    final_mem_estimate = (
        (len(branch_cache) + len(subject_cache) + len(teacher_cache)) * 80
        + preview_state["written"] * 64
    )
    if final_mem_estimate > peak_mem_estimate_bytes:
        peak_mem_estimate_bytes = final_mem_estimate

    elapsed_seconds = time.time() - start_ts
    return {
        "raw_insert": {"counters": raw_counters},
        "normalized_insert": {"counters": normalized_counters},
        "preview_path": preview_path if preview_state["written"] else None,
        "preview_written": preview_state["written"],
        "batch_commits": batch_commits,
        "elapsed_seconds": elapsed_seconds,
        "peak_memory_estimate_bytes": peak_mem_estimate_bytes,
    }

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
    skipped_rows_omitted = 0
    preview_path = os.path.join(os.path.dirname(__file__), "uploads", "last_import_debug.jsonl")
    os.makedirs(os.path.dirname(preview_path), exist_ok=True)
    preview_written = 0
    batch_commit_counts = 0
    progress_log_interval = max(50, BATCH_INSERT_SIZE)
    start_time = time.time()
    use_tracemalloc = False
    if ENABLE_IMPORT_TRACEMALLOC:
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
                skipped_rows_omitted = _append_skipped_sample(skipped_rows, skipped_rows_omitted, {"index": row_index, "raw": s, "normalized": row, "reason": reason})
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
                    skipped_rows_omitted = _append_skipped_sample(skipped_rows, skipped_rows_omitted, {"index": row_index, "raw": s, "normalized": row, "reason": reason})
                    continue
            except Exception:
                counters["normalization_failures"] += 1
                logger.exception("Duplicate check failed at row %s | row=%s", row_index, row)
                logger.error(traceback.format_exc())
                skipped_rows_omitted = _append_skipped_sample(skipped_rows, skipped_rows_omitted, {"index": row_index, "raw": s, "normalized": row, "reason": "dup_check_exception"})
                raise

            try:
                _db_execute(
                    db,
                    _insert_ignore_sql(db, "timetable_slots", ["branch", "section", "semester", "day", "start_time", "end_time", "subject_name", "faculty_name", "is_lab", "room"]),
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
                skipped_rows_omitted = _append_skipped_sample(skipped_rows, skipped_rows_omitted, {"index": row_index, "raw": s, "normalized": row, "reason": "insert_exception"})
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

    return {"counters": counters, "skipped_rows": skipped_rows, "skipped_rows_omitted": skipped_rows_omitted, "preview_path": preview_path if preview_written else None, "batch_commits": batch_commit_counts, "elapsed_seconds": time.time() - start_time, "memory_peak_bytes": peak}


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
    skipped_rows_omitted = 0
    preview_path = os.path.join(os.path.dirname(__file__), "uploads", "last_import_debug.jsonl")
    os.makedirs(os.path.dirname(preview_path), exist_ok=True)
    preview_written = 0
    batch_commit_counts = 0
    progress_log_interval = max(50, BATCH_INSERT_SIZE)
    start_time = time.time()
    use_tracemalloc = False
    if ENABLE_IMPORT_TRACEMALLOC:
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
                skipped_rows_omitted = _append_skipped_sample(skipped_rows, skipped_rows_omitted, {"index": row_index, "raw": s, "normalized": normalized_row, "reason": reason})
                continue
            try:
                _db_execute(
                    db,
                    _insert_ignore_sql(db, "timetable_entries", ["branch_id", "section", "semester", "day", "start_time", "end_time", "subject_id", "teacher_id", "is_lab", "room"]),
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
                skipped_rows_omitted = _append_skipped_sample(skipped_rows, skipped_rows_omitted, {"index": row_index, "raw": s, "normalized": normalized_row, "reason": "insert_exception"})
                raise

            teacher_id = None
            try:
                row = _db_execute(db, "SELECT id FROM teachers WHERE LOWER(name)=LOWER(%s) LIMIT 1", (fac_name,)).fetchone()
                teacher_id = row[0] if row and not hasattr(row, 'keys') else (row['id'] if row else None)
            except Exception:
                teacher_id = None

            if branch_id is None:
                branch_id = _resolve_or_create_branch_id(db, bname or sec or row.get("branch", ""), branch_cache)
                branch_cache[branch_key] = branch_id
            if branch_id is None:
                counters["skipped_total"] += 1
                counters["skipped_branch"] += 1
                reason = "missing_branch"
                logger.info("import_slots_normalized skipped unresolved branch for row %s: %s", row_index, normalized_row)
                skipped_rows_omitted = _append_skipped_sample(skipped_rows, skipped_rows_omitted, {"index": row_index, "raw": s, "normalized": normalized_row, "reason": reason})
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
                    skipped_rows_omitted = _append_skipped_sample(skipped_rows, skipped_rows_omitted, {"index": row_index, "raw": s, "normalized": normalized_row, "reason": reason})
                    continue
            except Exception:
                counters["normalization_failures"] += 1
                logger.exception("Duplicate check failed at row %s | row=%s", row_index, normalized_row)
                logger.error(traceback.format_exc())
                skipped_rows_omitted = _append_skipped_sample(skipped_rows, skipped_rows_omitted, {"index": row_index, "raw": s, "normalized": normalized_row, "reason": "dup_check_exception"})
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
                    _insert_ignore_sql(db, "timetable_entries", ["branch_id", "section", "semester", "day", "start_time", "end_time", "subject_id", "teacher_id", "is_lab", "room"]),
                    (row['branch_id'], row['section'], row['semester'], row['day'], row['start_time'], row['end_time'], row['subject_id'], row['teacher_id'], row['is_lab'], row['room']),
                )
                counters["inserted"] += 1
            except Exception:
                counters["failures"] += 1
                counters["normalization_failures"] += 1
                logger.exception("Import failed at normalized row %s | row=%s", row_index, normalized_row)
                logger.error(traceback.format_exc())
                skipped_rows_omitted = _append_skipped_sample(skipped_rows, skipped_rows_omitted, {"index": row_index, "raw": s, "normalized": normalized_row, "reason": "insert_exception"})
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

    return {"counters": counters, "skipped_rows": skipped_rows, "skipped_rows_omitted": skipped_rows_omitted, "preview_path": preview_path if preview_written else None, "batch_commits": batch_commit_counts, "elapsed_seconds": time.time() - start_time, "memory_peak_bytes": peak}


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
                slots_iter = None
                pdf_stats = None
                if ext in (".docx",) and docx is not None:
                    summary = scan_docx_structure(dest, max_tables=TIMETABLE_MAX_TABLES + 1)
                    section_names = summary.get("section_names") or []
                    if summary.get("table_count", 0) > TIMETABLE_MAX_TABLES:
                        flash(
                            f"This DOCX contains {summary.get('table_count')} tables. Upload one section per DOCX (CSE-A, CSE-B, etc.) for low-memory imports.",
                            "error",
                        )
                        return redirect(url_for("timetable_manage"))
                    if TIMETABLE_SINGLE_SECTION_ONLY and len(section_names) > 1:
                        preview = ", ".join(section_names[:4])
                        suffix = "..." if len(section_names) > 4 else ""
                        flash(
                            f"Multiple sections detected ({preview}{suffix}). Please upload one section per DOCX.",
                            "error",
                        )
                        return redirect(url_for("timetable_manage"))
                    if summary.get("timetable_tables", 0) == 0:
                        flash("No timetable tables were detected in this DOCX.", "error")
                        return redirect(url_for("timetable_manage"))
                    if summary.get("faculty_tables", 0) == 0:
                        flash("Faculty mapping table not detected. Ensure the DOCX includes the faculty table.", "error")
                        return redirect(url_for("timetable_manage"))
                    slots_iter = iter_docx_section_slots(
                        dest,
                        single_section_only=TIMETABLE_SINGLE_SECTION_ONLY,
                        max_tables=TIMETABLE_MAX_TABLES,
                    )
                elif ext in (".pdf",) and pdfplumber is not None:
                    pdf_stats = {}
                    slots_iter = parse_pdf_to_slots(dest, stats=pdf_stats)
                else:
                    flash("Unsupported file type or missing parser dependencies.", "error")
                    return redirect(url_for("timetable_manage"))

                import_info = import_slots_streaming(db, slots_iter)
                inserted_info = import_info.get("raw_insert", {})
                normalized_info = import_info.get("normalized_insert", {})
                i_c = inserted_info.get("counters", {}) if isinstance(inserted_info, dict) else {}
                if int(i_c.get("processed", 0) or 0) == 0:
                    logger.warning("Timetable import parsed zero rows from file=%s ext=%s", filename, ext)
                    if ext == ".pdf":
                        if pdf_stats and pdf_stats.get("validation_errors"):
                            detail = "; ".join(pdf_stats.get("validation_errors")[:3])
                            flash(f"PDF validation failed: {detail}", "error")
                        else:
                            flash(
                                "No timetable rows were parsed from the PDF. Ensure the PDF contains a timetable grid with a Day column, time slots, and a faculty mapping table below it.",
                                "error",
                            )
                    else:
                        flash(
                            "No timetable rows were parsed from the uploaded file. Check that the DOCX contains a readable table with branch, section, day, time, and subject columns.",
                            "error",
                        )
                    return redirect(url_for("timetable_manage"))

                # Persist a temporary preview of skipped rows for admin review
                preview = {
                    "raw_insert": inserted_info,
                    "normalized_insert": normalized_info,
                    "batch_commits": import_info.get("batch_commits", 0),
                    "elapsed_seconds": import_info.get("elapsed_seconds", 0),
                    "preview_path": import_info.get("preview_path"),
                    "preview_written": import_info.get("preview_written", 0),
                }
                if pdf_stats:
                    preview["pdf_stats"] = pdf_stats
                try:
                    preview_path = os.path.join(os.path.dirname(__file__), "uploads", "last_import_debug.json")
                    with open(preview_path, "w", encoding="utf-8") as f:
                        import json
                        json.dump(preview, f, indent=2, default=str)
                except Exception:
                    logger.exception("Failed to write import debug preview")

                n_c = normalized_info.get("counters", {}) if isinstance(normalized_info, dict) else {}
                flash(
                    f"Imported slots: processed={i_c.get('processed', 0)} inserted={i_c.get('inserted', 0)} skipped={i_c.get('skipped_total', 0)}. Normalized: processed={n_c.get('processed', 0)} inserted={n_c.get('inserted', 0)} skipped={n_c.get('skipped_total', 0)}. Batch commits={import_info.get('batch_commits', 0)}.",
                    "success",
                )
            except TimetablePDFValidationError as e:
                logger.warning("PDF validation failed: %s", str(e))
                flash(str(e), "error")
                if pdf_stats:
                    try:
                        preview_path = os.path.join(os.path.dirname(__file__), "uploads", "last_import_debug.json")
                        with open(preview_path, "w", encoding="utf-8") as f:
                            import json
                            json.dump({"pdf_stats": pdf_stats}, f, indent=2, default=str)
                    except Exception:
                        logger.exception("Failed to write PDF validation preview")
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
