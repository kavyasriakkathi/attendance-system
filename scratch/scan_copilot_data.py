import os
import psycopg2
import sys

DATABASE_URL = os.environ.get("DATABASE_URL", "")
BRANCH_ID = 16

def run_scan():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    
    print("--- DRY RUN REPORT ---")
    print("Branch ID: {}".format(BRANCH_ID))
    
    # Check if branch exists
    cur.execute("SELECT name FROM branches WHERE id = %s", (BRANCH_ID,))
    branch = cur.fetchone()
    if not branch:
        print("Branch with ID {} NOT FOUND.".format(BRANCH_ID))
        return
    
    print("Branch Name: {}\n".format(branch[0]))
    
    report = []
    total_deleted = 0
    
    # 1. Students directly assigned to this branch
    cur.execute("SELECT id FROM students WHERE branch_id = %s", (BRANCH_ID,))
    student_ids = [row[0] for row in cur.fetchall()]
    
    report.append({"table": "students", "rows": len(student_ids), "reason": "branch_id = 16"})
    total_deleted += len(student_ids)
    
    if student_ids:
        # 2. Find all tables with student_id column
        cur.execute("""
            SELECT table_name 
            FROM information_schema.columns 
            WHERE column_name = 'student_id' 
              AND table_schema = 'public'
        """)
        tables_with_student_id = [row[0] for row in cur.fetchall()]
        
        for table in tables_with_student_id:
            format_strings = ','.join(['%s'] * len(student_ids))
            query = f"SELECT COUNT(*) FROM {table} WHERE student_id IN ({format_strings})"
            cur.execute(query, tuple(student_ids))
            count = cur.fetchone()[0]
            if count > 0:
                report.append({"table": table, "rows": count, "reason": "student_id IN affected students"})
                total_deleted += count
                
    # 3. Find all tables with branch_id column (except students which is already covered)
    cur.execute("""
        SELECT table_name 
        FROM information_schema.columns 
        WHERE column_name = 'branch_id' 
          AND table_schema = 'public'
          AND table_name != 'students'
    """)
    tables_with_branch_id = [row[0] for row in cur.fetchall()]
    
    for table in tables_with_branch_id:
        query = f"SELECT COUNT(*) FROM {table} WHERE branch_id = %s"
        cur.execute(query, (BRANCH_ID,))
        count = cur.fetchone()[0]
        if count > 0:
            report.append({"table": table, "rows": count, "reason": "branch_id = 16"})
            total_deleted += count
            
    # Finally, the branch itself
    report.append({"table": "branches", "rows": 1, "reason": "id = 16"})
    total_deleted += 1
    
    print("Tables affected and number of rows to be deleted:")
    for item in report:
        print(f"- {item['table']}: {item['rows']} rows ({item['reason']})")
        
    print(f"\nTotal rows affected: {total_deleted}")
    conn.close()

if __name__ == '__main__':
    run_scan()
