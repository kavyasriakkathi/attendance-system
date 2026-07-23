"""
Tests for Pending Fee Assignments and Payment Recording
Senior Flask ERP Architect Level Test Suite
"""

import os
import sys
import pytest
import sqlite3
from datetime import date

# Add root project directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import _recalculate_assignment_status


@pytest.fixture
def test_db():
    """Create an in-memory database with fee schema for testing."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row

    db.execute("CREATE TABLE branches (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("CREATE TABLE students (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, enrollment TEXT, branch_id INTEGER, semester INTEGER, academic_year TEXT)")
    db.execute("CREATE TABLE fee_structures (id INTEGER PRIMARY KEY AUTOINCREMENT, fee_name TEXT, category TEXT, amount REAL, academic_year TEXT, semester INTEGER, due_date TEXT)")
    db.execute("CREATE TABLE student_fee_assignments (id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER, fee_structure_id INTEGER, custom_amount REAL, discount_amount REAL, due_date TEXT, status TEXT, UNIQUE(student_id, fee_structure_id))")
    db.execute("CREATE TABLE fee_payments (id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id INTEGER, student_id INTEGER, amount_paid REAL, payment_date TEXT, payment_mode TEXT, transaction_id TEXT, receipt_number TEXT, notes TEXT, recorded_by TEXT)")

    # Seed branch & student
    db.execute("INSERT INTO branches VALUES (1, 'CSE')")
    db.execute("INSERT INTO students VALUES (101, 'Siddheshwar', 'CSE001', 1, 1, '2025-2026')")

    # Seed fee structures
    db.execute("INSERT INTO fee_structures (id, fee_name, category, amount, academic_year, semester, due_date) VALUES (1, 'Tuition Fee', 'Tuition', 25000.0, '2025-2026', 1, '2026-12-31')")
    db.execute("INSERT INTO fee_structures (id, fee_name, category, amount, academic_year, semester, due_date) VALUES (2, 'Bus Fee', 'Transport', 8000.0, '2025-2026', 1, '2026-12-31')")
    db.execute("INSERT INTO fee_structures (id, fee_name, category, amount, academic_year, semester, due_date) VALUES (3, 'Library Fee', 'Library', 2000.0, '2025-2026', 1, '2026-12-31')")

    db.commit()
    yield db
    db.close()


def test_multiple_fee_assignments_creation(test_db):
    """Verify that multiple fee assignments can be created for the same student."""
    db = test_db
    # Assign Tuition Fee
    db.execute("INSERT INTO student_fee_assignments (student_id, fee_structure_id, status) VALUES (101, 1, 'Unpaid')")
    # Assign Bus Fee
    db.execute("INSERT INTO student_fee_assignments (student_id, fee_structure_id, status) VALUES (101, 2, 'Unpaid')")
    # Assign Library Fee
    db.execute("INSERT INTO student_fee_assignments (student_id, fee_structure_id, status) VALUES (101, 3, 'Unpaid')")
    db.commit()

    assignments = db.execute("SELECT * FROM student_fee_assignments WHERE student_id = 101").fetchall()
    assert len(assignments) == 3


def test_pending_assignments_query_returns_all_unpaid_and_partial(test_db):
    """Verify that query returns all unpaid and partially paid assignments for a student, excluding paid ones."""
    db = test_db
    # Tuition Fee: ₹25,000, paid ₹10,000 -> Partially Paid (Pending ₹15,000)
    db.execute("INSERT INTO student_fee_assignments (id, student_id, fee_structure_id, status) VALUES (1, 101, 1, 'Partially Paid')")
    db.execute("INSERT INTO fee_payments (assignment_id, student_id, amount_paid) VALUES (1, 101, 10000.0)")

    # Bus Fee: ₹8,000, paid ₹0 -> Unpaid (Pending ₹8,000)
    db.execute("INSERT INTO student_fee_assignments (id, student_id, fee_structure_id, status) VALUES (2, 101, 2, 'Unpaid')")

    # Library Fee: ₹2,000, paid ₹2,000 -> Paid (Pending ₹0)
    db.execute("INSERT INTO student_fee_assignments (id, student_id, fee_structure_id, status) VALUES (3, 101, 3, 'Paid')")
    db.execute("INSERT INTO fee_payments (assignment_id, student_id, amount_paid) VALUES (3, 101, 2000.0)")
    db.commit()

    sql = """
    SELECT sfa.id AS assignment_id, s.id AS student_id, s.name AS student_name, s.enrollment,
           fs.fee_name, fs.category, fs.academic_year, fs.semester,
           sfa.custom_amount, sfa.discount_amount, fs.amount AS default_amount, sfa.status,
           b.name AS branch_name, COALESCE(SUM(fp.amount_paid), 0) AS total_paid
    FROM student_fee_assignments sfa
    JOIN students s ON sfa.student_id = s.id
    JOIN fee_structures fs ON sfa.fee_structure_id = fs.id
    LEFT JOIN branches b ON s.branch_id = b.id
    LEFT JOIN fee_payments fp ON fp.assignment_id = sfa.id
    WHERE sfa.student_id = 101 AND (sfa.status IS NULL OR sfa.status != 'Paid')
    GROUP BY sfa.id, s.id, s.name, s.enrollment, fs.fee_name, fs.category, fs.academic_year, fs.semester, sfa.custom_amount, sfa.discount_amount, fs.amount, sfa.status, b.name
    ORDER BY fs.fee_name ASC
    """
    rows = db.execute(sql).fetchall()

    # Must return 2 pending assignments (Tuition Fee & Bus Fee), excluding Library Fee
    assert len(rows) == 2

    fee_names = [r["fee_name"] for r in rows]
    assert "Bus Fee" in fee_names
    assert "Tuition Fee" in fee_names
    assert "Library Fee" not in fee_names

    # Check calculated pending amounts
    for r in rows:
        gross = r["custom_amount"] if r["custom_amount"] is not None else r["default_amount"]
        net = max(0.0, gross - (r["discount_amount"] or 0.0))
        pending = max(0.0, net - r["total_paid"])

        if r["fee_name"] == "Tuition Fee":
            assert net == 25000.0
            assert r["total_paid"] == 10000.0
            assert pending == 15000.0
        elif r["fee_name"] == "Bus Fee":
            assert net == 8000.0
            assert r["total_paid"] == 0.0
            assert pending == 8000.0


def test_payment_updates_only_selected_assignment(test_db):
    """Verify that recording a payment updates only the targeted assignment and leaves others unchanged."""
    db = test_db
    # Assignment 1: Tuition Fee ₹25,000
    db.execute("INSERT INTO student_fee_assignments (id, student_id, fee_structure_id, status) VALUES (1, 101, 1, 'Unpaid')")
    # Assignment 2: Bus Fee ₹8,000
    db.execute("INSERT INTO student_fee_assignments (id, student_id, fee_structure_id, status) VALUES (2, 101, 2, 'Unpaid')")
    db.commit()

    # Record ₹10,000 payment for Assignment 1 ONLY
    db.execute(
        "INSERT INTO fee_payments (assignment_id, student_id, amount_paid, payment_date, payment_mode) VALUES (1, 101, 10000.0, '2026-07-23', 'Cash')"
    )
    _recalculate_assignment_status(db, 1)

    # Verify Assignment 1 is updated to Partially Paid
    asg1 = db.execute("SELECT status FROM student_fee_assignments WHERE id = 1").fetchone()
    assert asg1["status"] == "Partially Paid"

    # Verify Assignment 2 remains Unpaid with 0 payments
    asg2 = db.execute("SELECT status FROM student_fee_assignments WHERE id = 2").fetchone()
    assert asg2["status"] == "Unpaid"

    pay2 = db.execute("SELECT COUNT(*) AS c FROM fee_payments WHERE assignment_id = 2").fetchone()
    assert pay2["c"] == 0
