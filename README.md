# Institute Attendance App

## Setup

1. Open PowerShell in `c:\Users\kavya\OneDrive\Desktop\project 1`
2. Install dependencies:
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

## Run the app

Use the provided starter script:
```powershell
./run.bat
```

Then open your browser at:

- `http://localhost:8000`

## Default login

- Username: `admin`
- Password: `admin123`

## Notes

- This app uses `waitress` as the WSGI server on Windows.
- The first time it runs, it creates `attendance.db` automatically.
