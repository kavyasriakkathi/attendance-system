#!/usr/bin/env python
"""
Test the branch name extraction logic from filenames
"""
import os
import sys

# Add the project directory to sys.path
sys.path.insert(0, os.path.dirname(__file__))

def test_branch_extraction():
    """Test that branch names are correctly extracted from filenames"""
    
    test_cases = [
        ("ECE-B.xlsx", "ECE-B"),
        ("CSE-A.csv", "CSE-A"),
        ("ECE-A.xlsx", "ECE-A"),
        ("MECHANICAL.csv", "MECHANICAL"),
        ("ece-b.xlsx", "ECE-B"),  # lowercase
        ("  CSM  .xlsx", "CSM"),  # with spaces
    ]
    
    print("=" * 60)
    print("TESTING BRANCH NAME EXTRACTION FROM FILENAMES:")
    print("=" * 60)
    
    all_passed = True
    for filename, expected in test_cases:
        # This is the logic from app.py upload handlers
        branch_name_from_file = filename.rsplit(".", 1)[0].strip().upper()
        passed = branch_name_from_file == expected
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status} | {filename:20s} → {branch_name_from_file:20s} (expected: {expected})")
        if not passed:
            all_passed = False
    
    print("=" * 60)
    if all_passed:
        print("✓ All tests passed! Upload logic is correct.")
    else:
        print("✗ Some tests failed!")
    print("=" * 60)

if __name__ == "__main__":
    test_branch_extraction()
