@echo off
rem Starts the NewsMap app (if not already running) and opens it in your browser.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" server.py
ping -n 2 127.0.0.1 >nul
start "" http://127.0.0.1:8765
