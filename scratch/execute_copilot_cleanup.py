import psycopg2
import sys
import json

DATABASE_URL = "postgresql://neondb_owner:npg_tlI7cGRBogs1@ep-withered-math-apo99psx-pooler.c-7.us-east-1.aws.neon.tech/neondb?sslmode=require"
BRANCH_ID = 16

def run_cleanup():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False # Ensure manual transaction control
    cur = conn.cursor()
    
    try:
        # Load the backup to get exactly the 47 student IDs
        with open("scratch/backup_copilot_data.json", "r") as f:
            backup_data = json.load(f)
            
        student_ids = [s["id"] for s in backup_data["students"]]
        
        if len(student_ids) != 47:
            print("ERROR: Expected 47 student IDs in backup, found {}.".format(len(student_ids)))
            sys.exit(1)
            
        print("--- CLEANUP EXECUTION START ---")
        
        format_strings = ','.join(['%s'] * len(student_ids))
        
        # 1. Delete users
        cur.execute(f"DELETE FROM users WHERE student_id IN ({format_strings})", tuple(student_ids))
        users_deleted = cur.rowcount
        print(f"Users deleted: {users_deleted}")
        
        # 2. Delete students
        # Also limit by branch_id = 16 just to be doubly safe
        cur.execute(f"DELETE FROM students WHERE id IN ({format_strings}) AND branch_id = %s", tuple(student_ids) + (BRANCH_ID,))
        students_deleted = cur.rowcount
        print(f"Students deleted: {students_deleted}")
        
        # 3. Delete branch
        cur.execute("DELETE FROM branches WHERE id = %s", (BRANCH_ID,))
        branches_deleted = cur.rowcount
        print(f"Branches deleted: {branches_deleted}")
        
        # 4. Verify
        cur.execute("SELECT count(*) FROM students WHERE branch_id = %s", (BRANCH_ID,))
        remaining_students = cur.fetchone()[0]
        
        cur.execute(f"SELECT count(*) FROM users WHERE student_id IN ({format_strings})", tuple(student_ids))
        remaining_users = cur.fetchone()[0]
        
        print("\n--- VERIFICATION ---")
        print(f"Remaining students referencing branch_id 16: {remaining_students}")
        print(f"Remaining users for those students: {remaining_users}")
        
        if remaining_students == 0 and remaining_users == 0 and students_deleted == 47 and users_deleted == 47:
            conn.commit()
            print("\nTRANSACTION STATUS: Committed successfully.")
            print("No orphan references remain. Database integrity is preserved.")
        else:
            conn.rollback()
            print("\nTRANSACTION STATUS: ROLLED BACK.")
            print("Verification failed! The numbers did not match exactly.")
            
    except Exception as e:
        conn.rollback()
        print("\nTRANSACTION STATUS: ROLLED BACK (due to error).")
        print(f"Error: {e}")
    finally:
        conn.close()

if __name__ == '__main__':
    run_cleanup()
