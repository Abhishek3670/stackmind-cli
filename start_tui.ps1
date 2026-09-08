# StackMind Interactive TUI PowerShell Launcher
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ScriptDir ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    Write-Host "[ERROR] Virtual environment Python not found at: $PythonExe" -ForegroundColor Red
    Write-Host "Please ensure the .venv directory exists in the workspace." -ForegroundColor Yellow
    exit 1
}

& $PythonExe (Join-Path $ScriptDir "tui.py") @args
