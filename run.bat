@echo off
cd /d "%~dp0"
echo Starting Real-Time Attendance System...
echo Press Ctrl+C to stop the server
"%~dp0.venv\Scripts\python.exe" app.py
pause
;