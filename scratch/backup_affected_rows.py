import psycopg2
import json
import sys

DATABASE_URL = "postgresql://neondb_owner:npg_tlI7cGRBogs1@ep-withered-math-apo99psx-pooler.c-7.us-east-1.aws.neon.tech/neondb?sslmode=require"
BRANCH_ID = 16

def run_backup():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    
    backup_data = {
        "branches": [],
        "students": [],
        "users": []
    }
    
    # 1. Backup branch
    cur.execute("SELECT * FROM branches WHERE id = %s", (BRANCH_ID,))
    branch_cols = [desc[0] for desc in cur.description]
    branch_row = cur.fetchone()
    if branch_row:
        backup_data["branches"].append(dict(zip(branch_cols, branch_row)))
        
    # 2. Backup students
    cur.execute("SELECT * FROM students WHERE branch_id = %s", (BRANCH_ID,))
    student_cols = [desc[0] for desc in cur.description]
    students = cur.fetchall()
    
    student_ids = []
    for s in students:
        backup_data["students"].append(dict(zip(student_cols, s)))
        student_ids.append(s[0]) # assuming id is the first column, actually let's use dictionary index
        
    student_ids = [s["id"] for s in backup_data["students"]]
    
    # 3. Backup users
    if student_ids:
        format_strings = ','.join(['%s'] * len(student_ids))
        cur.execute(f"SELECT * FROM users WHERE student_id IN ({format_strings})", tuple(student_ids))
        user_cols = [desc[0] for desc in cur.description]
        users = cur.fetchall()
        for u in users:
            backup_data["users"].append(dict(zip(user_cols, u)))
            
    with open("scratch/backup_copilot_data.json", "w") as f:
        json.dump(backup_data, f, indent=4, default=str)
        
    print("Backup saved to scratch/backup_copilot_data.json")
    print(f"Backed up {len(backup_data['branches'])} branches, {len(backup_data['students'])} students, and {len(backup_data['users'])} users.")
    conn.close()

if __name__ == '__main__':
    run_backup()
