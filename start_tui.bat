@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title StackMind TUI - Option A Runner

set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE=%SCRIPT_DIR%.venv\Scripts\python.exe"
set "RUNNER=%TEMP%\stackmind_tui_runner_%RANDOM%.py"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Virtual environment Python not found at: %PYTHON_EXE%
    echo Please ensure the .venv directory exists in the workspace.
    pause
    exit /b 1
)

echo ===============================================================================
echo Starting StackMind TUI (Option A)...
echo ===============================================================================

(
echo import sys, os, tempfile
echo from pathlib import Path
echo if hasattr^(sys.stdout, "reconfigure"^): sys.stdout.reconfigure^(encoding="utf-8"^)
echo workspace_root = Path^(os.getcwd^(^)^).resolve^(^)
echo if str^(workspace_root^) not in sys.path: sys.path.insert^(0, str^(workspace_root^)^)
echo from validators.kernel.daemon import LocalDaemon
echo from validators.kernel.tui import DaemonClient, StackMindTuiAdapter, activity_line, contract_panel, diff_viewer, hitl_prompt, session_header, verification_matrix
echo try:
echo     from rich.console import Console; from rich.panel import Panel
echo     console = Console^(^)
echo     def print_banner^(t, s="bold cyan"^): console.print^(Panel^(t, style=s, expand=False^)^)
echo except ImportError:
echo     def print_banner^(t, s=""^): print^("\n" + "="*80 + "\n" + t + "\n" + "="*80^)
echo print_banner^("STACKMIND INTERACTIVE TUI (v3.2.0 GA)\nZero-Bypass Governed Runtime Demo", "bold green"^)
echo state_dir = Path^(tempfile.gettempdir^(^)^) / "stackmind_daemon_demo"
echo state_dir.mkdir^(parents=True, exist_ok=True^)
echo print^("[*] Starting Local JSON-RPC Daemon on background thread..."^)
echo daemon = LocalDaemon^(state_dir, port=8765^).start^(^)
echo print^(f"[+] Daemon running at: {daemon.url}"^)
echo try:
echo     client = DaemonClient^(daemon.url^)
echo     adapter = StackMindTuiAdapter^(client^)
echo     print^("[*] Initializing governed session with CONTRACT-01 boundaries..."^)
echo     session = adapter.command^(":new", agent="codex", provider="claude-3-5-sonnet", workspace=str^(workspace_root^), contract={"allow": ["validators/kernel/*", "cli/tui.py", "tests/test_tui_*"], "deny": [".sync/runtime/boot/*", "production_db/*"]}^)
echo     print_banner^(session_header^(session^), "bold blue"^)
echo     print^("\n[+] [CONTRACT BOUNDARY HUD (WO-007)]:"^)
echo     print^("-" * 60^)
echo     print^(contract_panel^(session.get^("contract", {}^)^)^)
echo     print^("-" * 60^)
echo     print^("\n[+] [ACTIVITY STREAM (Sequenced & Checkpointed)]:"^)
echo     print^("  " + activity_line^({"name": "tool_call.query_graph", "payload": {"symbol": "StackMindTuiAdapter"}}^)^)
echo     print^("  " + activity_line^({"name": "tool_call.read_file", "payload": {"path": "validators/kernel/tui/adapter.py"}}^)^)
echo     print^("  " + activity_line^({"name": "tool_call.write_file", "payload": {"path": "validators/learning/skill.py", "cancelled": True}}^)^)
echo     print^("\n[+] [HUMAN-IN-THE-LOOP (HITL) APPROVAL REQUIRED]:"^)
echo     print^("-" * 60^)
echo     print^(hitl_prompt^("apply_scratch_changeset", "validators/kernel/tui/views.py"^)^)
echo     print^("-" * 60^)
echo     sample_diff = "--- a/validators/kernel/tui/views.py\n+++ b/validators/kernel/tui/views.py\n@@ -1,5 +1,6 @@\n+# Verified Changeset\n def session_header(session):\n"
echo     print^("\n[+] [UNIFIED DIFF VIEWER]:"^)
echo     print^(diff_viewer^(sample_diff^)^)
echo     print^("\n[+] [6-DIMENSIONAL VERIFICATION MATRIX]:"^)
echo     dims = {"scope": True, "state": True, "ast": True, "behavioral": True, "security": True, "outcome": True}
echo     print^("-" * 60^)
echo     print^(verification_matrix^(dims^)^)
echo     print^("-" * 60^)
echo     print^("\n[PASS] Option A execution complete. Runtime is healthy."^)
echo finally:
echo     print^("\n[*] Stopping daemon..."^)
echo     daemon.stop^(^)
echo     print^("[+] Daemon stopped gracefully."^)
) > "%RUNNER%"

"%PYTHON_EXE%" "%RUNNER%"
set "EXIT_CODE=%ERRORLEVEL%"

if exist "%RUNNER%" del /f /q "%RUNNER%"

if %EXIT_CODE% neq 0 (
    echo.
    echo [ERROR] Process exited with error code %EXIT_CODE%.
)

echo.
pause
exit /b %EXIT_CODE%
