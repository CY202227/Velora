# Start the terminal with the project's interpreter.
Push-Location $PSScriptRoot
try {
    & "$PSScriptRoot/.venv/Scripts/python.exe" -m server.terminal @args
    $terminalExit = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $terminalExit
