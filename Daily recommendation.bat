@echo off
cd /d "%~dp0"
py src\app.py || python src\app.py
pause
