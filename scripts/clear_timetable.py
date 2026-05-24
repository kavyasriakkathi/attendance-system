import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import app, get_db


def _count(db, table_name: str) -> int:
    row = db.execute(f"SELECT COUNT(1) AS c FROM {table_name}").fetchone()
    if row is None:
        return 0
    try:
        return int(row["c"])
    except Exception:
        return int(row[0])


def clear_timetable() -> int:
    summary = {
        "before": {},
        "deleted": {},
        "after": {},
        "protected_tables": {},
    }

    protected = ["students", "attendance", "teachers", "users", "subjects"]
    with app.app_context():
        db = get_db()

        summary["before"]["timetable_entries"] = _count(db, "timetable_entries")
        summary["before"]["timetable_slots"] = _count(db, "timetable_slots")
        for table in protected:
            summary["protected_tables"][table] = _count(db, table)

        try:
            cur_slots = db.execute("DELETE FROM timetable_slots")
            deleted_slots = int(getattr(cur_slots, "rowcount", 0) or 0)

            db.commit()
        except Exception as exc:
            db.rollback()
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            return 1

        summary["deleted"]["timetable_entries"] = 0
        summary["deleted"]["timetable_slots"] = deleted_slots
        summary["after"]["timetable_entries"] = _count(db, "timetable_entries")
        summary["after"]["timetable_slots"] = _count(db, "timetable_slots")

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(clear_timetable())
