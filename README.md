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
The application now uses a persistent SQLite database stored in the `instance/` folder. This ensures that your branches, subjects, and student data remain safe even after logging out or restarting the server.

### 2. Email Notifications
Automatic email alerts are sent to students whose attendance falls below the threshold (default: 75%) for a specific subject.

To configure email:
1. Open the `.env` file.
2. Enter your Gmail address in `MAIL_USERNAME`.
3. Enter your Google **App Password** in `MAIL_PASSWORD`.
4. Ensure `MAIL_USE_TLS=True` and `MAIL_PORT=587`.

## Notes
- This app uses `waitress` as the WSGI server on Windows.
- The database (`attendance.db`) is stored in the `instance/` directory to ensure data is not lost.
- To enable email alerts, you must provide valid SMTP credentials in `.env`.
- Render auto-deploys changes from the `main` branch when connected to GitHub.
