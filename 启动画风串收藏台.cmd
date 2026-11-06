@echo off
cd /d "%~dp0"
py -3 -B app.py --open
if errorlevel 1 pause
