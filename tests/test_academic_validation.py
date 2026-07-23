"""
Tests for Academic Setup Validation Module
Senior ERP QA Architect Level Test Suite
"""

import os
import sys
import pytest

# Add root project directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from academic_setup_validator import AcademicSetupValidator, validate_staged_slots


def test_valid_timetable_slots_pass():
    """Test that a complete and conflict-free timetable setup passes validation."""
    valid_slots = [
        {
            "branch": "CSE",
            "section": "CSE-A",
            "semester": 1,
            "subject_name": "Data Structures",
            "faculty_name": "Dr. Alan Turing",
            "day": "Monday",
            "start_time": "09:00",
            "end_time": "10:00",
            "room": "101",
        },
        {
            "branch": "CSE",
            "section": "CSE-A",
            "semester": 1,
            "subject_name": "Python Programming",
            "faculty_name": "Prof. Guido van Rossum",
            "day": "Monday",
            "start_time": "10:00",
            "end_time": "11:00",
            "room": "101",
        },
        {
            "branch": "ECE",
            "section": "ECE-A",
            "semester": 2,
            "subject_name": "Circuit Theory",
            "faculty_name": "Dr. Nikola Tesla",
            "day": "Monday",
            "start_time": "09:00",
            "end_time": "10:00",
            "room": "201",
        },
    ]

    report = validate_staged_slots(valid_slots)

    assert report["can_import"] is True
    assert len(report["block_import"]["critical_errors"]) == 0
    assert "CSE" in report["pass"]["branches_detected"]
    assert "ECE" in report["pass"]["branches_detected"]
    assert "CSE-A" in report["pass"]["sections_detected"]
    assert "ECE-A" in report["pass"]["sections_detected"]
    assert "Data Structures" in report["pass"]["subjects_detected"]
    assert "Python Programming" in report["pass"]["subjects_detected"]
    assert "Dr. Alan Turing" in report["pass"]["faculty_detected"]
    assert "Prof. Guido van Rossum" in report["pass"]["faculty_detected"]
    assert report["pass"]["total_slots"] == 3


def test_incomplete_slot_blocks_import():
    """Test that a slot missing Branch, Section, Semester, Subject, or Faculty triggers BLOCK IMPORT."""
    incomplete_slots = [
        {
            "branch": "",  # Missing Branch
            "section": "CSE-A",
            "semester": 1,
            "subject_name": "Algorithms",
            "faculty_name": "Dr. Knuth",
            "day": "Tuesday",
            "start_time": "09:00",
            "end_time": "10:00",
        },
        {
            "branch": "CSE",
            "section": "CSE-B",
            "semester": 1,
            "subject_name": "Database Systems",
            "faculty_name": "",  # Missing Faculty
            "day": "Tuesday",
            "start_time": "10:00",
            "end_time": "11:00",
        },
    ]

    report = validate_staged_slots(incomplete_slots)

    assert report["can_import"] is False
    assert len(report["block_import"]["critical_errors"]) > 0
    err_text = " ".join(report["block_import"]["critical_errors"])
    assert "Missing mandatory fields" in err_text
    assert "Branch" in err_text or "Faculty" in err_text


def test_missing_faculty_and_placeholder_detection():
    """Test detection of missing/unassigned faculty placeholders (TBD, VACANT, N/A)."""
    slots_with_placeholders = [
        {
            "branch": "MECH",
            "section": "MECH-A",
            "semester": 3,
            "subject_name": "Thermodynamics",
            "faculty_name": "TBD",  # Placeholder
            "day": "Wednesday",
            "start_time": "09:00",
            "end_time": "10:00",
        },
        {
            "branch": "MECH",
            "section": "MECH-A",
            "semester": 3,
            "subject_name": "Fluid Mechanics",
            "faculty_name": "VACANT",  # Placeholder
            "day": "Wednesday",
            "start_time": "10:00",
            "end_time": "11:00",
        },
    ]

    report = validate_staged_slots(slots_with_placeholders)

    # Missing faculty should produce warnings
    warn_text = " ".join(report["warnings"]["missing_data"])
    assert "unassigned" in warn_text.lower() or "faculty" in warn_text.lower()


def test_invalid_section_format_blocks_import():
    """Test detection of malformed section identifiers (e.g. I--B, illegal characters)."""
    invalid_section_slots = [
        {
            "branch": "CIVIL",
            "section": "I--B",  # Malformed double-dash section
            "semester": 1,
            "subject_name": "Surveying",
            "faculty_name": "Eng. John",
            "day": "Thursday",
            "start_time": "09:00",
            "end_time": "10:00",
        }
    ]

    report = validate_staged_slots(invalid_section_slots)

    assert report["can_import"] is False
    err_text = " ".join(report["block_import"]["critical_errors"])
    assert "Invalid or malformed section" in err_text


def test_faculty_time_conflict_blocks_import():
    """Test that double-booking a faculty member at the same day & time across different classes triggers BLOCK IMPORT."""
    conflicting_faculty_slots = [
        {
            "branch": "CSE",
            "section": "CSE-A",
            "semester": 1,
            "subject_name": "Operating Systems",
            "faculty_name": "Dr. Linus",
            "day": "Friday",
            "start_time": "09:00",
            "end_time": "10:00",
            "room": "101",
        },
        {
            "branch": "ECE",
            "section": "ECE-B",  # Different class/section!
            "semester": 1,
            "subject_name": "Embedded Systems",
            "faculty_name": "Dr. Linus",  # Same faculty scheduled at the exact same time!
            "day": "Friday",
            "start_time": "09:00",
            "end_time": "10:00",
            "room": "202",
        },
    ]

    report = validate_staged_slots(conflicting_faculty_slots)

    assert report["can_import"] is False
    err_text = " ".join(report["block_import"]["critical_errors"])
    assert "FACULTY CONFLICT" in err_text
    assert "Dr. Linus" in err_text


def test_section_time_conflict_blocks_import():
    """Test that double-booking a section at the same day & time for two subjects triggers BLOCK IMPORT."""
    conflicting_section_slots = [
        {
            "branch": "EEE",
            "section": "EEE-A",
            "semester": 2,
            "subject_name": "Power Systems",
            "faculty_name": "Prof. Ampere",
            "day": "Monday",
            "start_time": "11:00",
            "end_time": "12:00",
        },
        {
            "branch": "EEE",
            "section": "EEE-A",  # Same section!
            "semester": 2,
            "subject_name": "Control Systems",  # Different subject scheduled at the exact same time!
            "faculty_name": "Prof. Volta",
            "day": "Monday",
            "start_time": "11:00",
            "end_time": "12:00",
        },
    ]

    report = validate_staged_slots(conflicting_section_slots)

    assert report["can_import"] is False
    err_text = " ".join(report["block_import"]["critical_errors"])
    assert "SECTION CONFLICT" in err_text
    assert "EEE-A" in err_text


def test_empty_period_blocks_import():
    """Test that a slot with day/time but empty subject is flagged as Empty Period and blocks import."""
    empty_period_slots = [
        {
            "branch": "CSE",
            "section": "CSE-A",
            "semester": 1,
            "subject_name": "",  # Empty Subject!
            "faculty_name": "Dr. Smith",
            "day": "Wednesday",
            "start_time": "14:00",
            "end_time": "15:00",
        }
    ]

    report = validate_staged_slots(empty_period_slots)

    assert report["can_import"] is False
    err_text = " ".join(report["block_import"]["critical_errors"])
    assert "Empty Period" in err_text or "Missing mandatory fields" in err_text
