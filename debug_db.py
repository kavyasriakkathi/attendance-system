from app import get_db

if __name__ == '__main__':
    db = get_db()
    print('Branches:')
    for b in db.execute('SELECT id, name FROM branches').fetchall():
        print(f'  {b["id"]}: {b["name"]}')
    print('Subjects:')
    for s in db.execute('SELECT id, name, branch_id FROM subjects').fetchall():
        print(f'  {s["id"]}: {s["name"]} (branch {s["branch_id"]})')
    db.close()