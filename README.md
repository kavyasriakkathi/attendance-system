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

## Features & Configuration

### 1. Database Persistence
This app supports both SQLite and PostgreSQL:

- **Local development (default):** SQLite file `attendance.db` stored next to `app.py`.
- **Production (Render):** PostgreSQL when `DATABASE_URL` is set.

Important: if you previously deployed using SQLite on Render without a persistent disk, your data would not survive redeploys. PostgreSQL is the recommended production database.

### 1.1 Debug “No Data Found” (Render)
After logging in as admin, open:

- `/admin/check-db`

This endpoint returns JSON with which database is active and row counts for the main tables. If counts are all `0`, you are connected to a fresh/empty database.

### 1.2 Restore/Migrate Old SQLite Data to Render PostgreSQL
If you still have your old `attendance.db` (SQLite) locally, you can copy it into Render PostgreSQL using the provided script:

1. Make sure your Render service is deployed at least once (so tables exist).
2. Get your Render Postgres **External Database URL**.
3. Run:
   ```powershell
   $env:DATABASE_URL = "<paste Render Postgres External Database URL>"
   $env:SQLITE_PATH = "attendance.db"
   .\.venv\Scripts\python.exe .\migrate_sqlite_to_postgres.py
   ```
4. Re-check `/admin/check-db` to confirm data counts.

### 1.3 Import From `scratch/data_export.json`
If you don't have the old `attendance.db`, but you DO have an export JSON file (for example `scratch/data_export.json`), you can import it into Render PostgreSQL:

```powershell
cd "c:\Users\kavya\OneDrive\Desktop\project 1"
$env:DATABASE_URL = "<paste Render Postgres External Database URL>"
$env:EXPORT_JSON = "scratch\data_export.json"
.\.venv\Scripts\python.exe .\import_data_export_json.py
```

This imports `branches`, `subjects`, `students`, `attendance` and also creates student `users` accounts (`username=enrollment`).

### 2. Email Notifications
Automatic email alerts are sent to students whose attendance falls below the threshold (default: 75%) for a specific subject.

To configure email:
1. Open the `.env` file.
2. Enter your Gmail address in `MAIL_USERNAME`.
3. Enter your Google **App Password** in `MAIL_PASSWORD`.
4. Ensure `MAIL_USE_TLS=True` and `MAIL_PORT=587`.

## Notes
- This app uses `waitress` as the WSGI server on Windows.
- Locally, the database (`attendance.db`) is stored in the project folder.
- To enable email alerts, you must provide valid SMTP credentials in `.env`.
- Render auto-deploys changes from the `main` branch when connected to GitHub.
