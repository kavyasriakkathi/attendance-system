from app import get_db

if __name__ == '__main__':
    db = get_db()
    # Update one record to Absent for better percentage demo
    db.execute("UPDATE attendance SET status='Absent' WHERE student_id=6 AND subject_id=1")
    db.commit()
    print('Updated one record to Absent')
    db.close()