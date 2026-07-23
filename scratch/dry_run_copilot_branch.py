import psycopg2
import sys

DATABASE_URL = "postgresql://neondb_owner:npg_tlI7cGRBogs1@ep-withered-math-apo99psx-pooler.c-7.us-east-1.aws.neon.tech/neondb?sslmode=require"
BRANCH_NAME = "COPILOT_BRANCH_d3169ca1"

def run_dry_run():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        print("--- DRY RUN REPORT for branch '{}' ---".format(BRANCH_NAME))
        
        # 1. Check if branch exists
        cur.execute("SELECT id, name FROM branches WHERE name = %s", (BRANCH_NAME,))
        branch = cur.fetchone()
        
        if not branch:
            print("Branch '{}' NOT FOUND in branches table.".format(BRANCH_NAME))
            return
            
        branch_id = branch[0]
        print("1. Branch found: id={} name='{}'".format(branch_id, branch[1]))
        
        # 2. Check students referencing this branch_id
        cur.execute("SELECT id, name, roll_no, enrollment FROM students WHERE branch_id = %s", (branch_id,))
        students = cur.fetchall()
        print("\n2. Students referencing this branch_id: (Total: {})".format(len(students)))
        student_ids = []
        for s in students:
            print("   - id={} name='{}' roll_no='{}' enrollment='{}'".format(s[0], s[1], s[2], s[3]))
            student_ids.append(s[0])
            
        # Also check students containing string directly if there's a branch name column (though usually it's branch_id)
        # Let's check table columns first for students
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'students'")
        student_columns = [row[0] for row in cur.fetchall()]
        if 'branch' in student_columns:
             cur.execute("SELECT id, name, roll_no, enrollment FROM students WHERE branch = %s", (BRANCH_NAME,))
             students_direct = cur.fetchall()
             print("\n   Students with direct branch name '{}': (Total: {})".format(BRANCH_NAME, len(students_direct)))
             for s in students_direct:
                 print("   - id={} name='{}' roll_no='{}' enrollment='{}'".format(s[0], s[1], s[2], s[3]))
                 if s[0] not in student_ids:
                     student_ids.append(s[0])
                     
        # 3. Check subjects
        cur.execute("SELECT id, name FROM subjects WHERE branch_id = %s", (branch_id,))
        subjects = cur.fetchall()
        print("\n3. Subjects referencing this branch_id: (Total: {})".format(len(subjects)))
        for s in subjects:
            print("   - id={} name='{}'".format(s[0], s[1]))
            
        # 4. Check timetable_entries
        # Note: timetable_entries might reference branch_id directly, or subjects.
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'timetable_entries'")
        tt_columns = [row[0] for row in cur.fetchall()]
        if 'branch_id' in tt_columns:
            cur.execute("SELECT id FROM timetable_entries WHERE branch_id = %s", (branch_id,))
            tt = cur.fetchall()
            print("\n4. Timetable entries referencing this branch_id: (Total: {})".format(len(tt)))
            for t in tt:
                 print("   - id={}".format(t[0]))
        
        # 5. Check attendance
        # Attendance references students and timetable_entries (or subjects). Let's check by student_id
        if student_ids:
            format_strings = ','.join(['%s'] * len(student_ids))
            cur.execute(f"SELECT id, student_id, date, status FROM attendance WHERE student_id IN ({format_strings})", tuple(student_ids))
            attendance = cur.fetchall()
            print("\n5. Attendance records referencing affected students: (Total: {})".format(len(attendance)))
            for a in attendance:
                 print("   - id={} student_id={} date='{}' status='{}'".format(a[0], a[1], a[2], a[3]))
        else:
            print("\n5. No affected students to check attendance for.")
            
        conn.close()
        
    except Exception as e:
        print("Error: ", e)

if __name__ == '__main__':
    run_dry_run()
