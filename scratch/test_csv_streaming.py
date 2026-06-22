import io
import csv

class MockFileStream(io.BytesIO):
    pass

def test_streaming():
    csv_data = b"name,enrollment,email,branch_id\nAlice,12345,alice@example.com,1\nBob,67890,bob@example.com,2\n"
    mock_file = MockFileStream(csv_data)
    
    # Simulate Werkzeug's file.stream wrapping in TextIOWrapper
    text_stream = io.TextIOWrapper(mock_file, encoding="utf-8-sig", errors="replace")
    reader = csv.DictReader(text_stream)
    
    print("Fieldnames:", reader.fieldnames)
    rows = list(reader)
    print("Rows:", rows)
    assert len(rows) == 2
    assert rows[0]["name"] == "Alice"
    assert rows[1]["name"] == "Bob"
    print("Test passed successfully!")

if __name__ == "__main__":
    test_streaming()
