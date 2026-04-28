from app import get_db, notify_low_attendance
import sqlite3

def test():
    db = get_db()
    
    # get a student
    student_id = 1
    
    # mark a student present/absent? 
    # let's just see what notify_low_attendance does
    emailed = notify_low_attendance(db, [1, 2, 3, 4, 5, 6])
    print("Emailed:", emailed)
    
if __name__ == "__main__":
    test()
