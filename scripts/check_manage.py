import os, sys, re
# Ensure project root is on path
PR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PR not in sys.path:
    sys.path.insert(0, PR)
from app import app, get_db

client = app.test_client()
# Perform login to become admin (default seeded admin:admin123)
login_data = {'username': 'admin', 'password': 'admin123'}
resp = client.post('/admin-login', data=login_data, follow_redirects=True)
print('login_status', resp.status_code)
resp = client.get('/timetable/manage', follow_redirects=True)
print('status_code', resp.status_code)
html = resp.get_data(as_text=True)
# crude parsing: count tables and <tr> occurrences
tables = re.findall(r'<table[^>]*>(.*?)</table>', html, flags=re.S|re.I)
print('tables_found', len(tables))
print('html_length', len(html))
print('\nHTML_SNIPPET_START:\n')
print(html[:2000])
print('\nHTML_SNIPPET_END\n')
rendered_table_rows = 0
for t in tables:
    # count TRs
    trs = re.findall(r'<tr[^>]*>', t, flags=re.I)
    # if the table has a thead, subtract its rows roughly by counting <th>
    thead = re.search(r'<thead.*?>(.*?)</thead>', t, flags=re.S|re.I)
    header_rows = 0
    if thead:
        header_rows = len(re.findall(r'<tr[^>]*>', thead.group(1), flags=re.I))
    # estimate body row count
    body_trs = len(trs) - header_rows
    if body_trs < 0:
        body_trs = 0
    rendered_table_rows += body_trs
print('rendered_row_count_estimate', rendered_table_rows)
# Show snippet of first table
if tables:
    snippet = tables[0][:1500]
    print('\nFIRST_TABLE_SNIPPET:\n')
    print(snippet)
# Print SQL used (replicated)
sql = '''SELECT
                te.day,
                te.start_time,
                te.end_time,
                te.section,
                te.semester,
                te.room,
                te.is_lab,
                COALESCE(s.name, '') AS subject_name,
                COALESCE(t.name, '') AS faculty_name,
                COALESCE(b.name, '') AS branch
            FROM timetable_entries te
            LEFT JOIN subjects s ON te.subject_id = s.id
            LEFT JOIN teachers t ON te.teacher_id = t.id
            LEFT JOIN branches b ON te.branch_id = b.id
            ORDER BY te.day, te.start_time'''
print('\nsql_used:')
print(sql)
# normalized rows count via DB
with app.app_context():
    db = get_db()
    try:
        c = db.execute('SELECT COUNT(1) FROM timetable_entries').fetchone()
        print('\nnormalized_rows_count_sql:', c[0] if c else 0)
    except Exception as e:
        print('normalized_rows_count_sql: error', e)

# print raw slots count
with app.app_context():
    db = get_db()
    try:
        c = db.execute('SELECT COUNT(1) FROM timetable_slots').fetchone()
        print('raw_slots_count_sql:', c[0] if c else 0)
    except Exception as e:
        print('raw_slots_count_sql: error', e)
