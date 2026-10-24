#!/bin/sh
# Starts the Lichess Opening Trainer on Linux. The first time, if python-chess
# isn't installed, it's installed into a private .venv folder next to this
# script (recent Debian/Ubuntu don't allow a plain "pip install").
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is needed to run the Lichess Opening Trainer."
    echo "Install it with your package manager, e.g.: sudo apt install python3"
    exit 1
fi

PYTHON=python3
if [ -x .venv/bin/python ]; then
    PYTHON=.venv/bin/python
elif ! python3 -c "import chess" >/dev/null 2>&1; then
    echo "Installing the python-chess package (first run only)..."
    if ! python3 -m venv .venv; then
        echo "Couldn't create a Python environment. Install venv support, e.g.:"
        echo "    sudo apt install python3-venv"
        echo "or install python-chess system-wide:  sudo apt install python3-chess"
        rm -rf .venv
        exit 1
    fi
    if ! .venv/bin/python -m pip install --quiet chess; then
        echo "Couldn't install python-chess. Check your internet connection and try again."
        rm -rf .venv
        exit 1
    fi
    PYTHON=.venv/bin/python
fi

exec "$PYTHON" trainer.py
