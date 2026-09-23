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

# This is also the one-time first-setup wizard for optional, credential-gated
# features (currently just QuickBooks) - it only runs here, the moment .env is
# first created, so it never nags you again on later launches. Turning a
# feature on/off later is just editing its settings in .env and restarting.
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp ".env.example" ".env"
        echo "A new .env file was created from .env.example."
        echo

        echo "------------------------------------------------------------------"
        echo " Optional feature: QuickBooks Online integration"
        echo "------------------------------------------------------------------"
        echo " Tracks credit-card charges and Pirate Ship shipping costs against"
        echo " your pallets automatically. Needs a free app registered at"
        echo " https://developer.intuit.com. It's OFF by default - safe to skip"
        echo " for now and turn on later by filling in the QUICKBOOKS_* settings"
        echo " in .env and restarting the bot."
        echo
        read -r -p "Enable QuickBooks integration now? [y/N]: " qb_choice
        case "$qb_choice" in
            y|Y)
                echo
                echo "Next steps:"
                echo "  1. Create an app at https://developer.intuit.com"
                echo "     (My Apps -> create an app -> Keys & OAuth)"
                echo "  2. Open .env and fill in QUICKBOOKS_CLIENT_ID and"
                echo "     QUICKBOOKS_CLIENT_SECRET from that app's Keys & OAuth page."
                echo "  3. Once the bot is running, a Pallet Admin runs"
                echo "     /finance connect-quickbooks in Discord to finish connecting."
                echo "  4. Set QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID in .env to the ONE card"
                echo "     account to watch (see the comment above it for where to find its ID)."
                echo "Opening .env in ${EDITOR:-nano} now so you can fill these in - and"
                echo "DISCORD_BOT_TOKEN/DISCORD_GUILD_ID below, if you haven't yet..."
                "${EDITOR:-nano}" ".env"
                ;;
            *)
                echo "QuickBooks integration left OFF - nothing else to do for it."
                ;;
        esac
        echo

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
