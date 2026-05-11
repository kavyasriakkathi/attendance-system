# PHASE 2 Testing Guide: Semester-wise Attendance Tracking

## Overview
PHASE 2 adds semester and academic year tracking to the attendance system, enabling:
- Multi-semester attendance records for each student
- Attendance filtering by semester and academic year  
- Analytics dashboard with granular filtering
- Per-semester attendance calculations

## Database Changes (Already Applied)

### New Attendance Columns
- `attendance.semester` (Integer, default: 1)
- `attendance.academic_year` (Integer, default: 1)
- `attendance.marked_at` (Timestamp)

### Updated Unique Constraint
```sql
UNIQUE (student_id, subject_id, date, period, semester, academic_year)
```

## Pre-Testing Checklist

- [ ] Backup database before running tests
- [ ] Ensure app.py is in root directory
- [ ] Verify all template files exist in templates/ directory
- [ ] Python environment configured with Flask, SQLite3
- [ ] Both SQLite and PostgreSQL databases available (optional: PostgreSQL)

## Test Scenario 1: Attendance Marking with Semester Tracking

### Steps:
1. **Start the application**
   ```bash
   python app.py
   ```
   Navigate to http://localhost:5000

2. **Login as teacher**
   - Use valid teacher credentials
   - Verify current semester/year displayed in dashboard

3. **Mark attendance**
   - Navigate to "Mark Attendance"
   - Select students and mark present/absent
   - Submit attendance
   - **Expected:** Attendance saved with student's current_semester and current_year

4. **Verify database**
   - Check attendance table for new records
   - Confirm `semester`, `academic_year`, `marked_at` fields populated
   ```sql
   SELECT * FROM attendance ORDER BY id DESC LIMIT 5;
   ```

---

## Test Scenario 2: Student Semester Filter Dashboard

### Steps:
1. **Login as student**
   - Use valid student credentials
   - Navigate to student dashboard

2. **View semester filters**
   - **Expected:** Dropdown for Semester (1-8) and Academic Year (1-4) visible
   - Default: "All Semesters" and "All Years" selected

3. **Filter by semester**
   - Select "Semester 2" from dropdown
   - **Expected:** Page auto-submits, dashboard shows only Semester 2 attendance
   - Attendance % should recalculate based on filtered records

4. **Filter by academic year**
   - Select "Year 1" from Academic Year dropdown
   - **Expected:** Dashboard shows only Year 1 + Semester 2 records
   - Verify combined filtering works

5. **Clear filters**
   - Select "All Semesters" and "All Years"
   - **Expected:** Dashboard shows attendance from all periods

---

## Test Scenario 3: Teacher Attendance Records with Filters

### Steps:
1. **Login as teacher**
   - Navigate to "My Attendance Records"

2. **Verify filter UI**
   - **Expected:** Three filter sections visible:
     - Student search
     - Semester dropdown (1-8)
     - Academic Year dropdown (1-4)

3. **Filter by semester**
   - Select "Semester 3"
   - Click "Filter"
   - **Expected:** Table shows only Semester 3 records

4. **Combine filters**
   - Enter student name in search
   - Select Semester 4
   - Click "Filter"
   - **Expected:** Results filtered by student AND semester

5. **Clear filters**
   - Click "Clear" button
   - **Expected:** All records shown again

---

## Test Scenario 4: Attendance Analytics Dashboard

### Steps:
1. **Login as teacher**
   - Click "Analytics" button in dashboard

2. **Verify analytics page loads**
   - **Expected:** Page title "Attendance Analytics" visible
   - Filter form with Subject, Branch, Semester, Academic Year dropdowns
   - Statistics table below

3. **Select filters**
   - Select a subject
   - Select a branch
   - Select "Semester 1"
   - Select "Year 1"
   - Click "Apply Filters"

4. **Verify statistics table**
   - **Expected:** Table shows students matching filters with:
     - Student Name
     - Enrollment
     - Classes Attended
     - Total Classes
     - Attendance % (colored: red <75%, blue 75-90%, green ≥90%)
     - Status badge (Low/Good/Excellent)

5. **Verify summary statistics**
   - **Expected:** Below table, summary cards show:
     - Total Students
     - Excellent count (≥90%)
     - Good count (75-90%)
     - Low count (<75%)

6. **Test different filter combinations**
   - Subject only
   - Branch only
   - Semester only
   - All filters together
   - **Expected:** Results update correctly for each combination

---

## Test Scenario 5: Multi-Semester Attendance Calculation

### Setup:
1. Create test student (enrollment: TEST-001)
2. Update student's current_semester to 2 and current_year to 1
3. Mark attendance for this student 5 times

### Steps:
1. **Check overall attendance**
   - Login as student
   - Dashboard shows 5 attended / ? total

2. **Add semester 1 data manually** (via database)
   - Insert 10 attendance records with semester=1
   - Update student current_semester to 1
   - Mark attendance 8 times with semester=1

3. **Verify per-semester calculations**
   - Filter dashboard by "Semester 1"
   - **Expected:** Shows 8 attended / 10 total
   - Filter by "Semester 2"
   - **Expected:** Shows 5 attended / 5 total
   - "All Semesters" should show combined (13/15)

---

## Test Scenario 6: Database Compatibility (SQLite vs PostgreSQL)

### SQLite Tests:
1. Use default SQLite database
2. Run scenarios 1-5
3. **Expected:** All operations work correctly

### PostgreSQL Tests (Optional):
1. Configure DATABASE environment variable:
   ```bash
   export DATABASE=postgresql://user:pass@localhost:5432/attendance_db
   ```
2. Ensure PostgreSQL installed and running
3. Run `python app.py` 
4. Execute scenarios 1-5
5. **Expected:** All operations work identically to SQLite

---

## Test Scenario 7: Backward Compatibility

### Verify Existing Data:
1. **Check old attendance records (without semester/year)**
   - Attendance marked before PHASE 2 should still exist
   - Default semester=1, academic_year=1

2. **Query old records**
   ```sql
   SELECT * FROM attendance WHERE semester IS NULL OR semester = 1;
   ```
   - **Expected:** Records visible with defaults applied

3. **Update old student records**
   - Find old student with no current_semester set
   - Mark new attendance for this student
   - **Expected:** Uses default semester=1

---

## Test Scenario 8: Edge Cases

### Empty Data:
1. Filter by semester with no records
   - **Expected:** "No attendance records found" message

### Null Values:
1. Manually set student.current_semester to NULL
2. Mark attendance
3. **Expected:** Uses default semester=1 gracefully

### Boundary Values:
1. Test semester values: 0, 1, 8, 9
2. Test academic_year values: 0, 1, 4, 5
3. **Expected:** Only valid values stored/filtered correctly

---

## Performance Testing (Optional)

### Load Test:
1. Add 1000 attendance records across multiple semesters
2. Load attendance analytics page
3. Apply multiple filters
4. **Expected:** Page loads within 2 seconds

### Query Performance:
```sql
-- Check index usage
EXPLAIN SELECT * FROM attendance WHERE semester=1 AND academic_year=1;
```

---

## Post-Testing Verification

### Database Integrity:
```sql
-- Verify all attendance records have semester/academic_year
SELECT COUNT(*) FROM attendance WHERE semester IS NULL OR academic_year IS NULL;
-- Expected: 0 rows

-- Verify unique constraint works
-- Try duplicate insert - should fail or update
```

### Template Rendering:
- All templates load without errors
- Dropdowns display all semesters/years
- Filters apply without JavaScript errors

### Navigation:
- All new buttons/links work
- Analytics page accessible from dashboard
- Back buttons navigate correctly

---

## Expected Issues & Solutions

| Issue | Solution |
|-------|----------|
| Semester dropdowns empty | Verify attendance records exist with semester values |
| Analytics page blank | Check database connectivity, verify attendance table populated |
| Filters not applying | Clear browser cache, check console for errors |
| Template not found errors | Verify both template locations have files |

---

## Commit Readiness Checklist

Before committing to GitHub:

- [ ] All test scenarios completed successfully
- [ ] No syntax errors in app.py or templates
- [ ] Database backward compatibility verified
- [ ] Both SQLite and PostgreSQL tested (SQLite required, PostgreSQL optional)
- [ ] All new features working as documented
- [ ] No console errors or exceptions
- [ ] Old attendance records still accessible
- [ ] New attendance marked with proper semester/year

## Testing Completion

Once all tests pass:

1. Run `git status` to see changes
2. Stage changes: `git add .`
3. Commit: `git commit -m "PHASE 2: Add semester-wise attendance tracking with analytics"`
4. Push: `git push origin main`

---

**Status:** Ready for testing ✓
**Estimated Time:** 30-45 minutes for full test suite
**Contact:** For issues, review conversation history or check database state
