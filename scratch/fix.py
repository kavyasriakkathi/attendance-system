import sys

def modify_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Wrap branches query
    old1 = '''        if not assigned_branches and row_get(teacher, "branch_id") is not None:
            branch_row = db.execute(
                f"SELECT id, name, location FROM branches WHERE id = {placeholder}",
                (row_get(teacher, "branch_id"),),
            ).fetchone()
            if branch_row:
                assigned_branches.append({
                    "id": row_get(branch_row, "id"),
                    "name": row_get(branch_row, "name"),
                    "location": row_get(branch_row, "location"),
                })'''
    new1 = '''        if not assigned_branches and row_get(teacher, "branch_id") is not None:
            try:
                branch_row = db.execute(
                    f"SELECT id, name, location FROM branches WHERE id = {placeholder}",
                    (row_get(teacher, "branch_id"),),
                ).fetchone()
                if branch_row:
                    assigned_branches.append({
                        "id": row_get(branch_row, "id"),
                        "name": row_get(branch_row, "name"),
                        "location": row_get(branch_row, "location"),
                    })
            except Exception:
                pass'''
                
    # Wrap subjects query
    old2 = '''        if not assigned_subjects and row_get(teacher, "subject_id") is not None:
            subject_row = db.execute(
                f"SELECT id, name, branch_id FROM subjects WHERE id = {placeholder}",
                (row_get(teacher, "subject_id"),),
            ).fetchone()
            if subject_row:
                assigned_subjects.append({
                    "id": row_get(subject_row, "id"),
                    "name": row_get(subject_row, "name"),
                    "branch_id": row_get(subject_row, "branch_id"),
                })'''
    new2 = '''        if not assigned_subjects and row_get(teacher, "subject_id") is not None:
            try:
                subject_row = db.execute(
                    f"SELECT id, name, branch_id FROM subjects WHERE id = {placeholder}",
                    (row_get(teacher, "subject_id"),),
                ).fetchone()
                if subject_row:
                    assigned_subjects.append({
                        "id": row_get(subject_row, "id"),
                        "name": row_get(subject_row, "name"),
                        "branch_id": row_get(subject_row, "branch_id"),
                    })
            except Exception:
                pass'''
                
    # Wrap current_branch query
    old3 = '''        if current_branch_id and not current_branch_name:
            branch_row = db.execute(
                f"SELECT id, name, location FROM branches WHERE id = {placeholder}",
                (current_branch_id,),
            ).fetchone()
            if branch_row:
                current_branch_name = row_get(branch_row, "name")'''
    new3 = '''        if current_branch_id and not current_branch_name:
            try:
                branch_row = db.execute(
                    f"SELECT id, name, location FROM branches WHERE id = {placeholder}",
                    (current_branch_id,),
                ).fetchone()
                if branch_row:
                    current_branch_name = row_get(branch_row, "name")
            except Exception:
                pass'''
                
    content = content.replace(old1, new1)
    content = content.replace(old2, new2)
    content = content.replace(old3, new3)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print("Done")

modify_file('app.py')
