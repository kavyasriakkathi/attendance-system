from pathlib import Path
import pytest
import hmac
import hashlib
import app as app_module

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_key_12345")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "rzp_test_secret_67890")

    app_module.RAZORPAY_KEY_ID = "rzp_test_key_12345"
    app_module.RAZORPAY_KEY_SECRET = "rzp_test_secret_67890"

    db_path = Path(tmp_path) / "test_fee_assignment_flow.db"
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

        # 1. Create Branch & Student
        db.execute("INSERT OR REPLACE INTO branches (id, name, location) VALUES (100, 'CSE-DEPT', 'Block B')")
        db.execute("INSERT OR REPLACE INTO students (id, name, enrollment, branch_id, email) VALUES (600, 'Multi Fee Student', 'ENR-MULTI-600', 100, 'multifee@test.com')")


        # 2. Create Fee Structures: Tuition Fee (ID=4) and Exam Fee (ID=5)
        db.execute("INSERT OR REPLACE INTO fee_structures (id, fee_name, category, amount, academic_year, semester, branch_id, due_date) VALUES (4, 'Tuition Fee 2026', 'Tuition', 45000.0, '2025-2026', 'Sem 1', 100, '2026-12-31')")
        db.execute("INSERT OR REPLACE INTO fee_structures (id, fee_name, category, amount, academic_year, semester, branch_id, due_date) VALUES (5, 'Exam Fee 2026', 'Exam', 2500.0, '2025-2026', 'Sem 1', 100, '2026-11-30')")

        db.commit()
    finally:
        try:
            db.close()
        except Exception:
            pass

    with app_module.app.test_client() as c:
        yield c


def test_student_portal_displays_all_matching_fee_structures(client):
    """Verify that both Tuition Fee and Exam Fee appear in the Student Portal even if initially missing from assignment table."""
    with client.session_transaction() as sess:
        sess['user_id'] = 600
        sess['student_id'] = 600
        sess['role'] = 'student'

    res = client.get('/student/fees')
    assert res.status_code == 200
    assert b'Tuition Fee 2026' in res.data
    assert b'Exam Fee 2026' in res.data
    assert b'45,000' in res.data or b'45000' in res.data
    assert b'2,500' in res.data or b'2500' in res.data


def test_multiple_fees_paid_and_unpaid_coexistence(client):
    """Verify paid fee status remains intact when multiple fees (Tuition + Exam Fee) exist."""
    with client.session_transaction() as sess:
        sess['user_id'] = 600
        sess['student_id'] = 600
        sess['role'] = 'student'

    # Trigger student fee portal to auto-assign structures
    client.get('/student/fees')

    db = app_module.get_db()
    try:
        # Get assignment IDs for Exam Fee (5) and Tuition Fee (4)
        exam_asg = db.execute("SELECT id FROM student_fee_assignments WHERE student_id = 600 AND fee_structure_id = 5").fetchone()
        tuition_asg = db.execute("SELECT id FROM student_fee_assignments WHERE student_id = 600 AND fee_structure_id = 4").fetchone()

        assert exam_asg is not None
        assert tuition_asg is not None

        exam_asg_id = exam_asg['id']
        tuition_asg_id = tuition_asg['id']
    finally:
        db.close()

    # Pay Exam Fee via Razorpay API
    c_res = client.post('/api/payment/create-order', json={'assignment_id': exam_asg_id, 'amount': 2500.0})
    order_id = c_res.get_json()['order_id']
    pay_id = "pay_exam_fee_99"
    sig = hmac.new(b"rzp_test_secret_67890", f"{order_id}|{pay_id}".encode('utf-8'), hashlib.sha256).hexdigest()

    client.post('/api/payment/verify', json={
        'razorpay_order_id': order_id,
        'razorpay_payment_id': pay_id,
        'razorpay_signature': sig,
        'assignment_id': exam_asg_id
    })

    # Reload Student Fee Portal
    portal_res = client.get('/student/fees')
    assert portal_res.status_code == 200
    # Exam Fee is now Paid
    assert b'Paid' in portal_res.data
    # Tuition Fee remains Unpaid and payable
    assert b'Tuition Fee 2026' in portal_res.data
    assert b'Pay Online' in portal_res.data


def test_parent_portal_displays_all_child_fees(client):
    """Verify parent portal displays both Tuition and Exam fees for child."""
    with client.session_transaction() as sess:
        sess['user_id'] = 700
        sess['student_id'] = 600
        sess['role'] = 'parent'

    res = client.get('/parent/fees')
    assert res.status_code == 200
    assert b'Tuition Fee 2026' in res.data
    assert b'Exam Fee 2026' in res.data
