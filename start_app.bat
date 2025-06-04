@echo off
ECHO Activating virtual environment...

REM Activate the virtual environment
call "%~dp0venv\Scripts\activate.bat"

ECHO Starting WalkingPad Controller...

REM Run the application
python "%~dp0run.py"

pause