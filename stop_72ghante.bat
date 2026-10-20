@echo off
rem Stops the 72 Ghante server (website http://localhost:8000)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING') do taskkill /PID %%p /F >nul 2>&1
echo 72 Ghante server stopped.
timeout /t 2 >nul
