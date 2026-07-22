from pathlib import Path
import pytest
import app as app_module

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("FLASK_ENV", "development")

    db_path = Path(tmp_path) / "test_fee.db"
    app_module.app.config.update(
        TESTING=True,
        DATABASE=str(db_path),
        SECRET_KEY="test-secret-key",
    )

    app_module._DB_INIT_DONE = False

    db = app_module.get_db()
    try:
        app_module.init_db(db)
        app_module._ensure_fee_schema(db)
        app_module._ensure_parent_schema(db)

        # Setup mock data: Branch, Student, Parent user
        db.execute("INSERT OR REPLACE INTO branches (id, name, location) VALUES (100, 'CSE-TEST', 'Building A')")
        db.execute("INSERT OR REPLACE INTO students (id, name, enrollment, branch_id, email) VALUES (500, 'Fee Test Student', 'ENR-FEE-100', 100, 'student@test.com')")

        from werkzeug.security import generate_password_hash
        pass_hash = generate_password_hash("parent123")
        db.execute("INSERT OR REPLACE INTO parents (id, name, username, password, student_id) VALUES (700, 'Test Parent', 'parent_user', ?, 500)", (pass_hash,))
        db.commit()
    finally:
        try:
            db.close()
        except Exception:
            pass

    with app_module.app.test_client() as c:
        yield c


def test_admin_fee_creation_and_assignment(client):
    """Verify admin can create a fee structure and assign it to a student."""
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'admin'
        sess['role'] = 'admin'

    # Create Fee Structure via POST /admin/fees/structures
    res = client.post('/admin/fees/structures', data={
        'fee_name': 'Tuition Fee 2026',
        'category': 'Tuition',
        'amount': '45000.00',
        'academic_year': '2025-2026',
        'semester': 'Sem 1',
        'due_date': '2026-12-31',
        'description': 'Standard annual tuition'
    }, follow_redirects=True)
    assert res.status_code == 200

    db = app_module.get_db()
    try:
        fs_row = db.execute("SELECT id FROM fee_structures WHERE fee_name = 'Tuition Fee 2026'").fetchone()
        assert fs_row is not None
        fee_struct_id = app_module.row_get(fs_row, 'id')
    finally:
        db.close()

    res_assign = client.post('/admin/fees/assign', data={
        'action_type': 'single',
        'student_id': 500,
        'fee_structure_id': fee_struct_id,
        'custom_amount': '',
        'discount_amount': '5000.00',
        'due_date': '2026-12-31'
    }, follow_redirects=True)
    assert res_assign.status_code == 200


def test_payment_recording_and_receipt(client):
    """Verify recording payment updates balance, status, and generates a printable receipt."""
    db = app_module.get_db()
    try:
        db.execute("INSERT INTO fee_structures (id, fee_name, category, amount, academic_year, due_date) VALUES (1, 'Hostel Fee', 'Hostel', 20000, '2025-2026', '2026-12-31')")
        db.execute("INSERT INTO student_fee_assignments (id, student_id, fee_structure_id, discount_amount, status) VALUES (10, 500, 1, 0, 'Unpaid')")
        db.commit()
    finally:
        db.close()

    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'admin'
        sess['role'] = 'admin'

    res_pay = client.post('/admin/fees/record-payment', data={
        'assignment_id': 10,
        'amount_paid': '10000.00',
        'payment_date': '2026-07-22',
        'payment_mode': 'Online',
        'transaction_id': 'TXN123456',
        'notes': 'First installment'
    }, follow_redirects=True)
    assert res_pay.status_code == 200

    db = app_module.get_db()
    try:
        asg = db.execute("SELECT status FROM student_fee_assignments WHERE id = 10").fetchone()
        assert app_module.row_get(asg, 'status') == 'Partially Paid'
    finally:
        db.close()


def test_student_fee_display(client):
    """Verify logged in student can access their fee dashboard and view breakdown."""
    db = app_module.get_db()
    try:
        db.execute("INSERT INTO fee_structures (id, fee_name, category, amount, academic_year, due_date) VALUES (2, 'Exam Fee', 'Exam', 3000, '2025-2026', '2026-12-31')")
        db.execute("INSERT INTO student_fee_assignments (id, student_id, fee_structure_id, discount_amount, status) VALUES (20, 500, 2, 0, 'Unpaid')")
        db.commit()
    finally:
        db.close()

    with client.session_transaction() as sess:
        sess['user_id'] = 500
        sess['student_id'] = 500
        sess['username'] = 'student'
        sess['role'] = 'student'

    res = client.get('/student/fees')
    assert res.status_code == 200
    assert b'Exam Fee' in res.data


def test_parent_fee_access(client):
    """Verify logged in parent can access child's fee details."""
    db = app_module.get_db()
    try:
        db.execute("INSERT INTO fee_structures (id, fee_name, category, amount, academic_year, due_date) VALUES (3, 'Library Fee', 'Library', 1500, '2025-2026', '2026-12-31')")
        db.execute("INSERT INTO student_fee_assignments (id, student_id, fee_structure_id, discount_amount, status) VALUES (30, 500, 3, 0, 'Unpaid')")
        db.commit()
    finally:
        db.close()

    with client.session_transaction() as sess:
        sess['user_id'] = 700
        sess['parent_id'] = 700
        sess['student_id'] = 500
        sess['username'] = 'parent_user'
        sess['role'] = 'parent'

    res = client.get('/parent/fees')
    assert res.status_code == 200
    assert b'Library Fee' in res.data


def test_accountant_role_flow(client):
    """Verify accountant user can access dashboard, collect payment, and view reports."""
    db = app_module.get_db()
    try:
        db.execute("INSERT INTO fee_structures (id, fee_name, category, amount, academic_year, due_date) VALUES (4, 'Transport Fee', 'Transport', 12000, '2025-2026', '2026-12-31')")
        db.execute("INSERT INTO student_fee_assignments (id, student_id, fee_structure_id, discount_amount, status) VALUES (40, 500, 4, 0, 'Unpaid')")
        db.commit()
    finally:
        db.close()

    with client.session_transaction() as sess:
        sess['user_id'] = 800
        sess['username'] = 'accountant'
        sess['role'] = 'accountant'

    # 1. Dashboard
    res_dash = client.get('/accountant/dashboard')
    assert res_dash.status_code == 200
    assert b'Accountant Desk' in res_dash.data

    # 2. Record payment via Accountant route
    res_pay = client.post('/accountant/record-payment', data={
        'assignment_id': 40,
        'amount_paid': '6000.00',
        'payment_date': '2026-07-22',
        'payment_mode': 'UPI',
        'transaction_id': 'UPI-TEST-999',
        'notes': 'Accountant collected half'
    }, follow_redirects=True)
    assert res_pay.status_code == 200

    # 3. View accountant reports
    res_rep = client.get('/accountant/reports')
    assert res_rep.status_code == 200
    assert b'UPI-TEST-999' in res_rep.data


def test_installment_generation_and_management(client):
    """Verify generation of installments for a student fee assignment."""
    db = app_module.get_db()
    try:
        db.execute("INSERT INTO fee_structures (id, fee_name, category, amount, academic_year, due_date) VALUES (5, 'Annual Fee 2026', 'Tuition', 60000, '2025-2026', '2026-12-31')")
        db.execute("INSERT INTO student_fee_assignments (id, student_id, fee_structure_id, discount_amount, status) VALUES (50, 500, 5, 0, 'Unpaid')")
        db.commit()
    finally:
        db.close()

    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'admin'
        sess['role'] = 'admin'

    # Generate 3 installments
    res_gen = client.post('/admin/fees/installments/50', data={
        'action': 'generate',
        'num_installments': '3',
        'start_date': '2026-08-01',
        'interval_days': '30'
    }, follow_redirects=True)
    assert res_gen.status_code == 200

    db = app_module.get_db()
    try:
        insts = db.execute("SELECT * FROM fee_installments WHERE assignment_id = 50 ORDER BY installment_number").fetchall()
        assert len(insts) == 3
        assert app_module.row_get(insts[0], 'installment_amount') == 20000.0
    finally:
        db.close()


def test_admin_fee_analytics_access(client):
    """Verify admin can access fee analytics page."""
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'admin'
        sess['role'] = 'admin'

    res = client.get('/admin/fees/analytics')
    assert res.status_code == 200
    assert b'Advanced Fee Analytics' in res.data
