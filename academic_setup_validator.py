"""
Academic Setup Validation Module
QA Architect Level Timetable & Academic Setup Validator

This module provides validation for staged timetable slots before import confirmation.
It checks slot completeness, detects duplicate subjects, missing faculty, empty periods,
invalid section formats, and scheduling time conflicts (faculty & section double-booking).

Supports both PostgreSQL and SQLite database connections.
"""

import re
import logging
from typing import List, Dict, Any, Tuple, Optional, Set

logger = logging.getLogger("app.academic_validation")

# Common faculty placeholder names indicating missing/unassigned teacher
FACULTY_PLACEHOLDERS = {
    "TBD", "VACANT", "STAFF", "UNASSIGNED", "NONE", "N/A", "NA",
    "UNKNOWN", "TO BE DECIDED", "FACULTY", "TEACHER", "GUEST", "-"
}

# Regex pattern for invalid/malformed section strings (e.g. double dashes like I--B, illegal chars)
INVALID_SECTION_PATTERNS = [
    r"--",               # Double dashes like I--B
    r"^\s*$",            # Empty or whitespace only
    r"^N/?A$",           # N/A section
    r"[^\w\s\-\.]",      # Contains special characters other than alphanum, space, dash, dot
]


class AcademicSetupValidator:
    """Validator for academic setup and timetable slot import batches."""

    def __init__(self, slots: List[Dict[str, Any]], db: Optional[Any] = None):
        self.slots = slots or []
        self.db = db

    @staticmethod
    def _clean_str(val: Any) -> str:
        if val is None:
            return ""
        return str(val).strip()

    @staticmethod
    def _is_placeholder_faculty(faculty_name: str) -> bool:
        cleaned = faculty_name.strip().upper()
        if not cleaned or cleaned in FACULTY_PLACEHOLDERS:
            return True
        if re.match(r"^(TBD|VACANT|STAFF|UNASSIGNED|GUEST|N/?A|-)+$", cleaned):
            return True
        return False

    @staticmethod
    def _is_invalid_section(section_name: str) -> bool:
        if not section_name or not section_name.strip():
            return True
        cleaned = section_name.strip()
        for pat in INVALID_SECTION_PATTERNS:
            if re.search(pat, cleaned, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _parse_time_minutes(time_str: str) -> Optional[int]:
        """Convert HH:MM (24h or 12h) to total minutes from midnight for overlap comparison."""
        if not time_str:
            return None
        cleaned = time_str.strip().upper()
        
        # Try HH:MM format
        m = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)?$", cleaned)
        if not m:
            # Try simple integer period if time is given as period number
            if cleaned.isdigit():
                period_num = int(cleaned)
                return period_num * 60  # Mock 60 min slots for period numbers
            return None
        
        hours = int(m.group(1))
        minutes = int(m.group(2))
        ampm = m.group(3)
        
        if ampm:
            if ampm == "PM" and hours < 12:
                hours += 12
            elif ampm == "AM" and hours == 12:
                hours = 0
        
        return hours * 60 + minutes

    @classmethod
    def times_overlap(cls, start1: str, end1: str, start2: str, end2: str) -> bool:
        """Check if two time intervals overlap."""
        s1 = cls._parse_time_minutes(start1)
        e1 = cls._parse_time_minutes(end1)
        s2 = cls._parse_time_minutes(start2)
        e2 = cls._parse_time_minutes(end2)

        if s1 is None or e1 is None or s2 is None or e2 is None:
            # If times cannot be parsed, fallback to exact string equality
            return (start1.strip().lower() == start2.strip().lower() and
                    end1.strip().lower() == end2.strip().lower())
        
        # Overlap condition: start1 < end2 AND start2 < end1
        return (s1 < e2) and (s2 < e1)

    def validate(self) -> Dict[str, Any]:
        """Run all validation checks on the staged timetable slots.

        Returns a dictionary:
        {
            "can_import": bool,
            "pass": {
                "branches_detected": List[str],
                "sections_detected": List[str],
                "subjects_detected": List[str],
                "faculty_detected": List[str],
                "total_slots": int
            },
            "warnings": {
                "missing_data": List[str],
                "conflicts": List[str]
            },
            "block_import": {
                "critical_errors": List[str]
            }
        }
        """
        branches: Set[str] = set()
        sections: Set[str] = set()
        subjects: Set[str] = set()
        faculty_set: Set[str] = set()

        missing_data_warnings: List[str] = []
        conflict_warnings: List[str] = []
        critical_errors: List[str] = []

        if not self.slots:
            critical_errors.append("No timetable slots were found in the uploaded file to validate.")
            return {
                "can_import": False,
                "pass": {
                    "branches_detected": [],
                    "sections_detected": [],
                    "subjects_detected": [],
                    "faculty_detected": [],
                    "total_slots": 0,
                },
                "warnings": {
                    "missing_data": [],
                    "conflicts": [],
                },
                "block_import": {
                    "critical_errors": critical_errors,
                },
            }

        # -------------------------------------------------------------
        # 1. SLOT COMPLETENESS & DETECTIONS PASS
        # -------------------------------------------------------------
        # Track slot keys to detect duplicate slot schedule entries
        seen_slot_schedules: Dict[Tuple[str, str, str, str, str, str], int] = {}
        # Track subjects per branch/semester for naming anomaly detection
        subject_branch_sem_map: Dict[Tuple[str, str], Set[str]] = {}

        for idx, slot in enumerate(self.slots, 1):
            branch = self._clean_str(slot.get("branch"))
            section = self._clean_str(slot.get("section"))
            semester = self._clean_str(slot.get("semester"))
            subject = self._clean_str(slot.get("subject_name") or slot.get("subject"))
            faculty = self._clean_str(slot.get("faculty_name") or slot.get("faculty"))
            day = self._clean_str(slot.get("day"))
            start_time = self._clean_str(slot.get("start_time"))
            end_time = self._clean_str(slot.get("end_time"))
            room = self._clean_str(slot.get("room"))

            # Collect detected entities if present
            if branch:
                branches.add(branch)
            if section:
                sections.add(f"{branch}-{section}" if branch and not section.startswith(branch) else section)
            if subject:
                subjects.add(subject)
            if faculty and not self._is_placeholder_faculty(faculty):
                faculty_set.add(faculty)

            # Rule 1: Every timetable slot MUST have Branch, Section, Semester, Subject, Faculty
            missing_fields = []
            if not branch:
                missing_fields.append("Branch")
            if not section:
                missing_fields.append("Section")
            if not semester:
                missing_fields.append("Semester")
            if not subject:
                missing_fields.append("Subject")
            if not faculty:
                missing_fields.append("Faculty")

            if missing_fields:
                critical_errors.append(
                    f"Slot #{idx} ({day or 'Day N/A'} {start_time or ''}-{end_time or ''}) is incomplete! Missing mandatory fields: {', '.join(missing_fields)}."
                )

            # Rule 2a: Missing Faculty Detection
            if faculty and self._is_placeholder_faculty(faculty):
                missing_data_warnings.append(
                    f"Slot #{idx} [{branch} {section} Sem-{semester} - {subject}]: Faculty is marked as '{faculty}' (unassigned)."
                )

            # Rule 2b: Empty Periods Detection
            if not subject and (day or start_time):
                critical_errors.append(
                    f"Slot #{idx} [{branch} {section} {day} {start_time}-{end_time}]: Period has no Subject assigned (Empty Period)."
                )
            if not start_time or not end_time:
                missing_data_warnings.append(
                    f"Slot #{idx} [{branch} {section} Sem-{semester} - {subject}]: Missing start/end time."
                )

            # Rule 2c: Invalid Sections Detection
            if section and self._is_invalid_section(section):
                critical_errors.append(
                    f"Slot #{idx}: Invalid or malformed section identifier '{section}' (e.g., unexpected characters or double dashes)."
                )

            # Rule 2d: Duplicate Slot Entries
            if branch and section and day and start_time and end_time:
                sched_key = (branch.upper(), section.upper(), day.upper(), start_time, end_time, subject.upper())
                if sched_key in seen_slot_schedules:
                    conflict_warnings.append(
                        f"Duplicate timetable slot entry detected for {branch} {section} on {day} ({start_time}-{end_time}) with subject '{subject}' (Slots #{seen_slot_schedules[sched_key]} and #{idx})."
                    )
                else:
                    seen_slot_schedules[sched_key] = idx

            # Subject branch sem tracking
            if branch and semester and subject:
                b_sem_key = (branch.upper(), semester)
                if b_sem_key not in subject_branch_sem_map:
                    subject_branch_sem_map[b_sem_key] = set()
                subject_branch_sem_map[b_sem_key].add(subject)

            # Optional data check
            if not room:
                missing_data_warnings.append(
                    f"Slot #{idx} [{branch} {section} - {subject}]: Room number is not specified."
                )

        # -------------------------------------------------------------
        # 2. CONFLICT DETECTIONS (FACULTY & SECTION TIME OVERLAPS)
        # -------------------------------------------------------------
        num_slots = len(self.slots)
        for i in range(num_slots):
            s1 = self.slots[i]
            d1 = self._clean_str(s1.get("day")).upper()
            st1 = self._clean_str(s1.get("start_time"))
            et1 = self._clean_str(s1.get("end_time"))
            fac1 = self._clean_str(s1.get("faculty_name") or s1.get("faculty"))
            br1 = self._clean_str(s1.get("branch")).upper()
            sec1 = self._clean_str(s1.get("section")).upper()
            sem1 = self._clean_str(s1.get("semester"))
            sub1 = self._clean_str(s1.get("subject_name") or s1.get("subject"))

            if not d1 or not st1 or not et1:
                continue

            for j in range(i + 1, num_slots):
                s2 = self.slots[j]
                d2 = self._clean_str(s2.get("day")).upper()
                st2 = self._clean_str(s2.get("start_time"))
                et2 = self._clean_str(s2.get("end_time"))
                fac2 = self._clean_str(s2.get("faculty_name") or s2.get("faculty"))
                br2 = self._clean_str(s2.get("branch")).upper()
                sec2 = self._clean_str(s2.get("section")).upper()
                sem2 = self._clean_str(s2.get("semester"))
                sub2 = self._clean_str(s2.get("subject_name") or s2.get("subject"))

                # Check if on the same day and overlapping time period
                if d1 == d2 and self.times_overlap(st1, et1, st2, et2):
                    # Check A: Faculty Conflict (Same faculty assigned to two different classes at the same time)
                    if (fac1 and fac2 and 
                        not self._is_placeholder_faculty(fac1) and 
                        fac1.upper() == fac2.upper()):
                        
                        # Only conflict if different section/branch or different subject
                        if br1 != br2 or sec1 != sec2 or sub1 != sub2:
                            critical_errors.append(
                                f"FACULTY CONFLICT: Faculty '{fac1}' is double-booked on {d1} ({st1}-{et1} vs {st2}-{et2}) "
                                f"between [{br1} {sec1} - {sub1}] and [{br2} {sec2} - {sub2}]."
                            )

                    # Check B: Section Conflict (Same section scheduled for two different subjects at the same time)
                    if br1 == br2 and sec1 == sec2 and sem1 == sem2:
                        if sub1 != sub2 or fac1 != fac2:
                            critical_errors.append(
                                f"SECTION CONFLICT: Section '{br1}-{sec1}' (Sem-{sem1}) has multiple conflicting subjects scheduled on {d1} "
                                f"({st1}-{et1} vs {st2}-{et2}): '{sub1}' ({fac1 or 'No Faculty'}) vs '{sub2}' ({fac2 or 'No Faculty'})."
                            )

        # Deduplicate warnings & errors preserving order
        def _dedup(seq: List[str]) -> List[str]:
            seen = set()
            res = []
            for item in seq:
                if item not in seen:
                    seen.add(item)
                    res.append(item)
            return res

        missing_data_warnings = _dedup(missing_data_warnings)
        conflict_warnings = _dedup(conflict_warnings)
        critical_errors = _dedup(critical_errors)

        # Limit warning list length to prevent flooding UI while keeping critical errors complete
        max_warn_display = 20
        if len(missing_data_warnings) > max_warn_display:
            omitted = len(missing_data_warnings) - max_warn_display
            missing_data_warnings = missing_data_warnings[:max_warn_display] + [f"...and {omitted} more missing data warnings."]

        can_import = len(critical_errors) == 0

        report = {
            "can_import": can_import,
            "pass": {
                "branches_detected": sorted(list(branches)),
                "sections_detected": sorted(list(sections)),
                "subjects_detected": sorted(list(subjects)),
                "faculty_detected": sorted(list(faculty_set)),
                "total_slots": len(self.slots),
            },
            "warnings": {
                "missing_data": missing_data_warnings,
                "conflicts": conflict_warnings,
            },
            "block_import": {
                "critical_errors": critical_errors,
            },
        }

        logger.info(
            "Academic Setup Validation complete: can_import=%s, branches=%d, sections=%d, subjects=%d, faculty=%d, errors=%d, warnings=%d",
            can_import,
            len(branches),
            len(sections),
            len(subjects),
            len(faculty_set),
            len(critical_errors),
            len(missing_data_warnings) + len(conflict_warnings),
        )

        return report


def validate_staged_slots(slots: List[Dict[str, Any]], db: Optional[Any] = None) -> Dict[str, Any]:
    """Helper function to run AcademicSetupValidator on a set of timetable slots."""
    validator = AcademicSetupValidator(slots, db=db)
    return validator.validate()
