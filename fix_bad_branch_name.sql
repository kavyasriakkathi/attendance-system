-- Migration: Fix COPILOT_BRANCH_d3169ca1 → ECE-B
-- This script renames the bad branch name and updates all references

-- Step 1: Check if the bad branch exists
-- SELECT id, name FROM branches WHERE name = 'COPILOT_BRANCH_d3169ca1';

-- Step 2: Check if ECE-B already exists
-- SELECT id, name FROM branches WHERE name = 'ECE-B';

-- Step 3a: If ECE-B exists, merge the data
-- Update all students pointing to bad branch to use ECE-B
UPDATE students 
SET branch_id = (SELECT id FROM branches WHERE name = 'ECE-B')
WHERE branch_id = (SELECT id FROM branches WHERE name = 'COPILOT_BRANCH_d3169ca1');

-- Update all subjects
UPDATE subjects 
SET branch_id = (SELECT id FROM branches WHERE name = 'ECE-B')
WHERE branch_id = (SELECT id FROM branches WHERE name = 'COPILOT_BRANCH_d3169ca1');

-- Update all timetable_entries
UPDATE timetable_entries 
SET branch_id = (SELECT id FROM branches WHERE name = 'ECE-B')
WHERE branch_id = (SELECT id FROM branches WHERE name = 'COPILOT_BRANCH_d3169ca1');

-- Update all attendance records
UPDATE attendance 
SET branch_id = (SELECT id FROM branches WHERE name = 'ECE-B')
WHERE branch_id = (SELECT id FROM branches WHERE name = 'COPILOT_BRANCH_d3169ca1');

-- Update all teacher_branches
UPDATE teacher_branches 
SET branch_id = (SELECT id FROM branches WHERE name = 'ECE-B')
WHERE branch_id = (SELECT id FROM branches WHERE name = 'COPILOT_BRANCH_d3169ca1');

-- Update all teachers
UPDATE teachers 
SET branch_id = (SELECT id FROM branches WHERE name = 'ECE-B')
WHERE branch_id = (SELECT id FROM branches WHERE name = 'COPILOT_BRANCH_d3169ca1');

-- Step 4: Delete the bad branch record
DELETE FROM branches WHERE name = 'COPILOT_BRANCH_d3169ca1';

-- Step 5: Verify the fix
SELECT COUNT(*) as student_count 
FROM students 
WHERE branch_id = (SELECT id FROM branches WHERE name = 'ECE-B');

SELECT * FROM branches WHERE name = 'ECE-B';
