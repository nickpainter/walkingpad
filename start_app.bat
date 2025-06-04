@echo off
ECHO Activating virtual environment...

REM Activate the virtual environment
call "%~dp0.venv\Scripts\activate.bat"

ECHO Starting WalkingPad Controller...

REM Run the application
python "%~dp0run.py"

pause