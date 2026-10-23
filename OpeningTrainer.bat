@echo off
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo Python 3 is needed to run the Lichess Opening Trainer.
    echo Install it from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH" during setup, then run this again.
    pause
    exit /b 1
)

python -c "import chess" >nul 2>&1
if errorlevel 1 (
    echo Installing the python-chess package ^(first run only^)...
    python -m pip install chess
    if errorlevel 1 (
        echo Couldn't install python-chess. Try running: python -m pip install chess
        pause
        exit /b 1
    )
)

python trainer.py
if errorlevel 1 pause
