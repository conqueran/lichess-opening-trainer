@echo off
cd /d "%~dp0"

if not exist "trainer.py" (
    echo The trainer's files aren't next to this launcher.
    echo If you opened the zip, extract it first: right-click the zip,
    echo choose "Extract All...", then run OpeningTrainer.bat from the
    echo extracted "Lichess Opening Trainer" folder.
    pause
    exit /b 1
)

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
