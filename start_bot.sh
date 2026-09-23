#!/usr/bin/env bash
# Linux/macOS launcher for the pallet tracking bot - the shell equivalent of
# start_bot.bat. Sets up the virtual environment/dependencies if needed,
# sanity-checks .env, then runs the bot; on a crash it asks whether to
# restart instead of just exiting.
set -uo pipefail

# Always run from the folder this script is in.
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "============================================"
echo " Almighty Pallet Bot - Startup"
echo "============================================"
echo

PYTHON_CMD="$(command -v python3 || command -v python || true)"
if [ -z "$PYTHON_CMD" ]; then
    echo "[ERROR] Python was not found on PATH."
    echo "Install Python 3.11 or newer (python3.org, or your OS's package manager)."
    exit 1
fi

if [ ! -x "venv/bin/python" ]; then
    echo "Creating a virtual environment in ./venv ..."
    "$PYTHON_CMD" -m venv venv || { echo "[ERROR] Failed to create the virtual environment."; exit 1; }
fi

echo "Installing/updating dependencies..."
venv/bin/python -m pip install --upgrade pip >/dev/null
venv/bin/python -m pip install -r requirements.txt || { echo "[ERROR] Failed to install dependencies."; exit 1; }

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp ".env.example" ".env"
        echo "A new .env file was created from .env.example."
        echo "Open .env and fill in DISCORD_BOT_TOKEN and DISCORD_GUILD_ID,"
        echo "then run this script again."
        exit 1
    else
        echo "[ERROR] No .env file found, and no .env.example to copy from."
        echo "Create a .env file with at least DISCORD_BOT_TOKEN set before continuing."
        exit 1
    fi
fi

if ! grep -qE '^DISCORD_BOT_TOKEN=.+' ".env"; then
    echo "[ERROR] DISCORD_BOT_TOKEN is missing or empty in .env."
    echo "Open .env and set it to your bot's token from the Discord Developer Portal"
    echo "(Developer Portal -> your application -> Bot -> Token)."
    exit 1
fi

echo
echo "Starting the bot. Press Ctrl+C to stop it."
echo "============================================"
echo

while true; do
    venv/bin/python bot.py
    exit_code=$?

    echo
    echo "============================================"
    if [ "$exit_code" -eq 0 ]; then
        echo "The bot exited normally."
        exit 0
    fi

    echo "The bot stopped with exit code $exit_code. Scroll up to see the error -"
    echo "a common one is \"another bot process already holds the lock\", which"
    echo "means an instance is already running somewhere against this same data"
    echo "folder."
    echo
    read -r -p "Press [r] to restart the bot, or any other key to exit: " choice
    case "$choice" in
        r|R) continue ;;
        *) exit "$exit_code" ;;
    esac
done
