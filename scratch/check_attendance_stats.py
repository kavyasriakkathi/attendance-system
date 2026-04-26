import sqlite3
import os

def check_attendance():
    basedir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    db_path = os.path.join(basedir, "attendance.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    rows = conn.execute("SELECT branch_id, COUNT(*) as count FROM attendance GROUP BY branch_id").fetchall()
    print("Attendance per branch:")
    for row in rows:
        print(f"  Branch {row['branch_id']}: {row['count']} records")
        
    branches = conn.execute("SELECT id, name FROM branches").fetchall()
    branch_ids_with_attendance = [row['branch_id'] for row in rows]
    
    print("\nBranches with NO attendance:")
    for b in branches:
        if b['id'] not in branch_ids_with_attendance:
            print(f"  {b['id']}: {b['name']}")
            
    conn.close()

if __name__ == "__main__":
    check_attendance()
