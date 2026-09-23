@echo off
setlocal enabledelayedexpansion

:: Windows launcher for the pallet tracking bot - double-click this (or make
:: a Desktop shortcut to it) instead of typing commands by hand every time.
:: It sets up the virtual environment/dependencies if needed, sanity-checks
:: your .env, runs the bot, and keeps the window open on exit so you can
:: actually read any error instead of it flashing shut.

:: Always run from the folder this script is in, so it works whether it's
:: launched by double-click, a Desktop shortcut, or Task Scheduler.
cd /d "%~dp0"

:: Windows consoles default to a legacy codepage that can't display this
:: bot's emoji-laden log messages and would otherwise crash Python with a
:: UnicodeEncodeError on the first one - switch to UTF-8 before printing
:: anything, and also force Python's own UTF-8 mode as a second guarantee
:: regardless of the console's codepage.
chcp 65001 >nul
set PYTHONUTF8=1

echo ============================================
echo  Almighty Pallet Bot - Startup
echo ============================================
echo.

:: --- Find a Python interpreter to create the virtual environment with ---
set PYTHON_CMD=
where python >nul 2>nul
if not errorlevel 1 set PYTHON_CMD=python
if not defined PYTHON_CMD (
    where py >nul 2>nul
    if not errorlevel 1 set PYTHON_CMD=py
)
if not defined PYTHON_CMD (
    echo [ERROR] Python was not found on PATH.
    echo Install Python 3.11 or newer from https://www.python.org/downloads/
    echo and make sure "Add python.exe to PATH" is checked during setup.
    echo.
    pause
    exit /b 1
)

:: --- Create the virtual environment the first time this is run ---
if not exist "venv\Scripts\python.exe" (
    echo Creating a virtual environment in .\venv ...
    %PYTHON_CMD% -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment. See the message above.
        pause
        exit /b 1
    )
)

:: --- Install/update dependencies every run, so a requirements.txt change ---
:: --- (e.g. after pulling an update) is never silently missed ------------
echo Installing/updating dependencies...
venv\Scripts\python.exe -m pip install --upgrade pip >nul
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies. See the message above.
    pause
    exit /b 1
)

:: --- Make sure a .env file exists, offering to create one from the example ---
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo A new .env file was created from .env.example.
        echo Open .env in a text editor and fill in DISCORD_BOT_TOKEN and
        echo DISCORD_GUILD_ID, then run this script again.
        echo.
        pause
        exit /b 1
    ) else (
        echo [ERROR] No .env file found, and no .env.example to copy from.
        echo Create a .env file with at least DISCORD_BOT_TOKEN set before continuing.
        pause
        exit /b 1
    )
)

:: --- Basic sanity check that DISCORD_BOT_TOKEN is actually filled in ---
findstr /r /c:"^DISCORD_BOT_TOKEN=..*" ".env" >nul
if errorlevel 1 (
    echo [ERROR] DISCORD_BOT_TOKEN is missing or empty in .env.
    echo Open .env and set it to your bot's token from the Discord Developer Portal
    echo ^(Developer Portal -^> your application -^> Bot -^> Token^).
    echo.
    pause
    exit /b 1
)

echo.
echo Starting the bot. Close this window or press Ctrl+C to stop it.
echo ============================================
echo.

:run
venv\Scripts\python.exe bot.py
set EXITCODE=%ERRORLEVEL%

echo.
echo ============================================
if "%EXITCODE%"=="0" (
    echo The bot exited normally.
    pause
    exit /b 0
)

echo The bot stopped with exit code %EXITCODE%. Scroll up to see the error -
echo a common one is "another bot process already holds the lock", which
echo means an instance is already running somewhere against this same data
echo folder.
echo.
choice /c RX /n /m "Press [R] to restart the bot, or [X] to close this window: "
if errorlevel 2 exit /b %EXITCODE%
if errorlevel 1 goto run
