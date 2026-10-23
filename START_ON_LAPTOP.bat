@echo off
rem One-click start for 72 Ghante on any laptop (first run installs everything, about 2-3 minutes).
cd /d "%~dp0"
where py >nul 2>nul || (echo Python is not installed. Install it from python.org, tick "Add to PATH", then run this again. & pause & exit /b)
if not exist ".venv\Scripts\python.exe" (
  echo First time setup, please wait...
  py -m venv .venv
  .venv\Scripts\python -m pip install fastapi "uvicorn[standard]" httpx python-dotenv pillow imageio-ffmpeg python-multipart google-genai
)
if not exist ".env" echo WARNING: .env file missing - AI replies will not work. Copy .env from the main PC into this folder.
start "" http://localhost:8000/
echo 72 Ghante is running. Keep this window open. Farmer phone: http://localhost:8000/static/phone.html
.venv\Scripts\python -m uvicorn app.main:app --port 8000
pause
