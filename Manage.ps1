param([ValidateSet('Setup','Check','Initialize','Preview','Start','Test')][string]$Action = 'Check')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtimePath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if ($Action -eq 'Setup') {
    if (-not (Test-Path -LiteralPath $runtimePath)) {
        & py -3.12 -m venv (Join-Path $PSScriptRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 for Windows first, including the Python launcher.' }
    }
    & $runtimePath -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. No bot was started.' }
    & $runtimePath (Join-Path $PSScriptRoot 'setup_wizard.py')
} elseif (-not (Test-Path -LiteralPath $runtimePath)) {
    throw 'Run Setup first.'
} elseif ($Action -eq 'Check') {
    & $runtimePath (Join-Path $PSScriptRoot 'combined_bot.py')
} elseif ($Action -eq 'Initialize') {
    & $runtimePath (Join-Path $PSScriptRoot 'combined_bot.py') --initialize
} elseif ($Action -eq 'Preview') {
    & $runtimePath (Join-Path $PSScriptRoot 'combined_bot.py') --connect --sync-commands
} elseif ($Action -eq 'Start') {
    & $runtimePath (Join-Path $PSScriptRoot 'combined_bot.py') --connect --send --sync-commands
} elseif ($Action -eq 'Test') {
    & $runtimePath -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements-dev.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Test dependencies could not be installed.' }
    & $runtimePath -m pytest tests -q
}
if ($LASTEXITCODE -ne 0) { throw 'The requested action did not complete. Read the message above.' }
