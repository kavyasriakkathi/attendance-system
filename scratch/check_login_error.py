import sqlite3

def run():
    conn = sqlite3.connect('attendance.db')
    conn.row_factory = sqlite3.Row
    db = conn.cursor()
    username = 't1' # we should probably find a real teacher username
    
    # First, let's find a teacher username
    user = db.execute("SELECT * FROM users WHERE role = 'teacher' LIMIT 1").fetchone()
    if not user:
        print("No teacher users found")
        return
        
    username = user['username']
    password = 'password' # we can bypass hash check for this test
    
    try:
        placeholder = "?"
        user = db.execute(
            f"SELECT id, username, password, role FROM users WHERE username = {placeholder}",
            (username,),
        ).fetchone()

        if user and user["role"] == "teacher":
            teacher_id = user["id"]
            
            try:
                assigned = db.execute(f"SELECT id, name FROM teachers WHERE id = {placeholder}", (teacher_id,)).fetchone()
                print(f"Teacher id used: {teacher_id}")
                if assigned:
                    print(f"Assigned name: {assigned['name']}")
                else:
                    print("Assigned is None")
            except Exception as e:
                print(f"Error querying teachers: {repr(e)}")
                
    except Exception as e:
        print(f"Login ERROR: {repr(e)}")
        
run()
