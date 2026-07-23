import sqlite3
import sys
import os

# Add root directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from timetable import auto_setup_academic_from_slots

# Dummy sqlite database connection for testing
conn = sqlite3.connect(":memory:")

# Setup minimal tables
conn.executescript("""
CREATE TABLE IF NOT EXISTS branches (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);
CREATE TABLE IF NOT EXISTS sections (id INTEGER PRIMARY KEY AUTOINCREMENT, branch_id INTEGER, name TEXT);
CREATE TABLE IF NOT EXISTS subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, code TEXT, branch_id INTEGER);
CREATE TABLE IF NOT EXISTS teachers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, username TEXT, password TEXT, password_hash TEXT, status TEXT, branch_id INTEGER, subject_id INTEGER);
CREATE TABLE IF NOT EXISTS teacher_subjects (teacher_id INTEGER, subject_id INTEGER, PRIMARY KEY (teacher_id, subject_id));
CREATE TABLE IF NOT EXISTS teacher_branches (teacher_id INTEGER, branch_id INTEGER, PRIMARY KEY (teacher_id, branch_id));
CREATE TABLE IF NOT EXISTS teacher_subject_assignments (id INTEGER PRIMARY KEY AUTOINCREMENT, teacher_id INTEGER, subject_id INTEGER, branch_id INTEGER, section TEXT, semester TEXT, academic_year TEXT);
""")

sample_slots = [
    {
        "branch": "CSE",
        "section": "CSE-A",
        "semester": "1",
        "subject_name": "Data Structures",
        "faculty_name": "Dr. Alan Turing",
        "day": "Monday",
        "start_time": "09:00",
        "end_time": "10:00"
    },
    {
        "branch": "CSE",
        "section": "CSE-B",
        "semester": "1",
        "subject_name": "Algorithms",
        "faculty_name": "Dr. Donald Knuth",
        "day": "Monday",
        "start_time": "10:00",
        "end_time": "11:00"
    },
    {
        "branch": "ECE",
        "section": "ECE-A",
        "semester": "2",
        "subject_name": "Digital Circuits",
        "faculty_name": "Prof. Claude Shannon",
        "day": "Tuesday",
        "start_time": "09:00",
        "end_time": "10:00"
    }
]

print("=== FIRST IMPORT (NEW ENTITIES) ===")
summary1 = auto_setup_academic_from_slots(conn, sample_slots)

print("\n=== SECOND IMPORT (REUSING ENTITIES) ===")
summary2 = auto_setup_academic_from_slots(conn, sample_slots)

conn.close()
