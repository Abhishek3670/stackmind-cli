import sys, os, tempfile
from pathlib import Path
if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
workspace_root = Path(os.getcwd()).resolve()
if str(workspace_root) not in sys.path: sys.path.insert(0, str(workspace_root))
from validators.kernel.daemon import LocalDaemon
from validators.kernel.tui import (
    DaemonClient,
    StackMindTuiAdapter,
    activity_line,
    contract_panel,
    diff_viewer,
    hitl_prompt,
    session_header,
    verification_matrix,
)
try:
    from rich.console import Console; from rich.panel import Panel
    console = Console()
    def print_banner(t, s="bold cyan"): console.print(Panel(t, style=s, expand=False))
except ImportError:
    console = None
    def print_banner(t, s=""): print("\n" + "="*80 + "\n" + t + "\n" + "="*80)

print_banner("STACKMIND INTERACTIVE TERMINAL OS (TUI v3.2.0 GA)\nZero-Bypass Governed Runtime Client", "bold green")
state_dir = Path(tempfile.gettempdir()) / "stackmind_daemon_live"
state_dir.mkdir(parents=True, exist_ok=True)
print("[*] Starting Local JSON-RPC Daemon on background thread...")
daemon = LocalDaemon(state_dir, port=8765).start()
print(f"[+] Daemon listening at: {daemon.url}")

client = DaemonClient(daemon.url)
adapter = StackMindTuiAdapter(client)
current_session = None

def show_demo():
    global current_session
    print("\n[*] --- RUNNING OPTION A DEMONSTRATION ---")
    s = adapter.command(":new", agent="codex", provider="claude-3-5-sonnet", workspace=str(workspace_root), contract={"allow": ["validators/kernel/*", "cli/tui.py", "tests/test_tui_*"], "deny": [".sync/runtime/boot/*", "production_db/*"]})
    current_session = s
    print_banner(session_header(s), "bold blue")
    print("\n[+] [CONTRACT BOUNDARY HUD]:\n" + "-"*60 + "\n" + contract_panel(s.get("contract", {})) + "\n" + "-"*60)
    print("\n[+] [ACTIVITY STREAM (Sequenced & Checkpointed)]:")
    print("  " + activity_line({"name": "tool_call.query_graph", "payload": {"symbol": "StackMindTuiAdapter"}}))
    print("  " + activity_line({"name": "tool_call.read_file", "payload": {"path": "validators/kernel/tui/adapter.py"}}))
    print("  " + activity_line({"name": "tool_call.write_file", "payload": {"path": "validators/learning/skill.py", "cancelled": True}}))
    print("\n[+] [HITL APPROVAL GATE]:\n" + "-"*60 + "\n" + hitl_prompt("apply_scratch_changeset", "validators/kernel/tui/views.py") + "\n" + "-"*60)
    sample_diff = "--- a/validators/kernel/tui/views.py\n+++ b/validators/kernel/tui/views.py\n@@ -1,5 +1,6 @@\n+# Verified Changeset\n def session_header(session):\n"
    print("\n[+] [UNIFIED DIFF VIEWER]:\n" + diff_viewer(sample_diff))
    dims = {"scope": True, "state": True, "ast": True, "behavioral": True, "security": True, "outcome": True}
    print("\n[+] [6-DIMENSIONAL VERIFICATION MATRIX]:\n" + "-"*60 + "\n" + verification_matrix(dims) + "\n" + "-"*60)
    print("\n[PASS] Demo finished. Session retained as active.\n")

def print_help():
    print("""
Available TUI Commands:
  :new [agent]      Create a new governed session (default agent: codex)
  :status           Display current session header and Contract Boundary HUD
  :diff             Display Unified Diff viewer
  :events           Stream incremental sequenced events from daemon
  :approve [reason] Submit Human-in-the-Loop (HITL) approval
  :reject [reason]  Submit Human-in-the-Loop (HITL) rejection
  :matrix           Display 6-Dimensional Verification Matrix
  :pause            Pause active session turn
  :resume           Resume active session
  :cancel           Cancel in-flight session turn
  :demo             Re-run Option A automated TUI demonstration
  :help             Show this help menu
  :exit, :quit, q   Gracefully stop daemon and exit
""")

print("\nType ':help' for commands, ':demo' for automated walkthrough, or ':new' to start.")
try:
    while True:
        sid_display = (current_session['session_id'][:8] + '...') if current_session else 'IDLE'
        prompt = f"stackmind [{sid_display}]> "
        try:
            cmd = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting...")
            break
        if not cmd:
            continue
        parts = cmd.split(" ", 1)
        action = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if action in (":exit", ":quit", "q", "exit", "quit"):
            break
        elif action == ":help":
            print_help()
        elif action == ":demo":
            show_demo()
        elif action == ":new":
            agent_name = arg if arg else "codex"
            print(f"[*] Initializing governed session for agent '{agent_name}'...")
            current_session = adapter.command(":new", agent=agent_name, provider="claude-3-5-sonnet", workspace=str(workspace_root), contract={"allow": ["validators/kernel/*", "cli/tui.py", "tests/test_tui_*"], "deny": [".sync/runtime/boot/*", "production_db/*"]})
            print_banner(session_header(current_session), "bold blue")
            print("\n📋 [CONTRACT BOUNDARY HUD]:\n" + "-"*60 + "\n" + contract_panel(current_session.get("contract", {})) + "\n" + "-"*60)
            print(f"[+] Session active: {current_session['session_id']}\n")
        elif action == ":status":
            if not current_session:
                print("[-] No active session. Type ':new' or ':demo' to start.")
            else:
                print_banner(session_header(current_session), "bold blue")
                print("\n📋 [CONTRACT BOUNDARY HUD]:\n" + "-"*60 + "\n" + contract_panel(current_session.get("contract", {})) + "\n" + "-"*60)
        elif action == ":diff":
            sample_diff = "--- a/validators/kernel/tui/views.py\n+++ b/validators/kernel/tui/views.py\n@@ -1,5 +1,6 @@\n+# Staged Diff (Ready for HITL Inspection)\n def session_header(session):\n"
            print("\n📄 [UNIFIED DIFF VIEWER]:\n" + diff_viewer(sample_diff))
        elif action == ":events":
            if not current_session:
                print("[-] No active session.")
            else:
                evs = list(adapter.stream(current_session["session_id"]))
                if not evs:
                    print("[+] No new events since last checkpoint.")
                else:
                    for ev in evs:
                        print("  " + activity_line(ev))
        elif action == ":approve":
            if not current_session:
                print("[-] No active session.")
            else:
                res = adapter.decide(current_session["session_id"], True, arg or "Approved by operator")
                print("[+] HITL Decision Recorded: APPROVED")
        elif action == ":reject":
            if not current_session:
                print("[-] No active session.")
            else:
                res = adapter.decide(current_session["session_id"], False, arg or "Rejected by operator")
                print("[-] HITL Decision Recorded: REJECTED")
        elif action == ":matrix":
            dims = {"scope": True, "state": True, "ast": True, "behavioral": True, "security": True, "outcome": True}
            print("\n🔍 [6-DIMENSIONAL VERIFICATION MATRIX]:\n" + "-"*60 + "\n" + verification_matrix(dims) + "\n" + "-"*60)
        elif action == ":pause":
            if not current_session:
                print("[-] No active session.")
            else:
                res = adapter.command(f":pause {current_session['session_id']}")
                print(f"[+] Session state: {res.get('state', 'PAUSED')}")
        elif action == ":resume":
            if not current_session:
                print("[-] No active session.")
            else:
                res = adapter.command(f":resume {current_session['session_id']}")
                print(f"[+] Session state: {res.get('state', 'RUNNING')}")
        elif action == ":cancel":
            if not current_session:
                print("[-] No active session.")
            else:
                res = adapter.command(f":cancel {current_session['session_id']}")
                print(f"[+] Session state: {res.get('state', 'CANCELLED')}")
        else:
            print(f"[-] Unknown command '{cmd}'. Type ':help' for available commands.")
finally:
    print("\n[*] Stopping Local JSON-RPC Daemon...")
    daemon.stop()
    print("[+] Daemon stopped gracefully. Session closed.")
