@echo off
cd /d "%~dp0"
python -m takeout_rescue
if errorlevel 1 pause
