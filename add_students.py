from app import get_db

if __name__ == '__main__':
    db = get_db()
    db.execute("INSERT OR IGNORE INTO students (id,name,enrollment,branch_id,email) VALUES (5, 'John Doe', 'E005', 1, 'john@example.com')")
    db.execute("INSERT OR IGNORE INTO students (id,name,enrollment,branch_id,email) VALUES (6, 'Jane Smith', 'E006', 1, 'jane@example.com')")
    db.commit()
    print('Added test students')
    db.close()