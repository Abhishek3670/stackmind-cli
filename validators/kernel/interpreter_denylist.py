"""Hard interpreter denylist for the agent-facing command boundary.

Blocks invocation of shells and general-purpose interpreters capable of
executing arbitrary code from a single string argument (bash -c, python -c,
node -e, ...). This exists to constrain untrusted agent-issued commands
specifically; trusted platform-internal callers (canary verifier, evidence
tracer) construct their own ProcessSandbox directly and are unaffected.

This is a HARD denylist: there is no per-contract override, consistent with
the project's fail-closed philosophy (CONTRACT-01). Legitimate shell needs
(piping, redirection) must go through a future per-command contract scoping
mechanism (Phase 4), not through loosening this gate.
"""

from __future__ import annotations

import re
from typing import Sequence

# Shells: arbitrary code execution via -c/-Command and command chaining.
_SHELLS = frozenset({
    "bash", "sh", "zsh", "ksh", "dash", "ash", "fish", "csh", "tcsh",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "wsl", "wsl.exe", "bash.exe", "sh.exe", "zsh.exe", "powershell_ise", "powershell_ise.exe",
})

# Interpreters blocked ONLY when invoked with a string-code flag; running a
# script file (python script.py) stays allowed.
_FLAGGED_INTERPRETERS = frozenset({
    "python", "python3", "pythonw", "pythonw.exe", "python.exe", "python3.exe",
    "pypy", "pypy3", "pypy.exe", "pypy3.exe",
    "perl", "perl.exe",
    "ruby", "ruby.exe",
    "node", "node.exe",
    "deno", "deno.exe",
    "bun", "bun.exe",
    "php", "php.exe",
    "lua", "lua.exe",
    "luajit", "luajit.exe",
    "jshell",
    "ghci",
    "tclsh",
    "wish",
})

# Flags that take a string of code for the interpreters above.
_CODE_FLAGS = frozenset({
    "-c", "--command",       # python/perl/ruby/php/lua -c
    "-e",                    # perl/ruby/node -e
    "--eval", "-p", "-n",    # perl -p/-n one-liners; node --eval
    "-Command", "-EncodedCommand",
})

# Interpreters blocked regardless of flags: their scripting model is a string
# program (or they exist to spawn other shells).
_UNCONDITIONAL = frozenset({
    "awk", "gawk", "mawk", "sed",
    "nc", "netcat", "ncat", "socat",
})


_EVAL = frozenset({"eval", "exec", "source", "."})

# Lowercased views for membership checks (exe names fold case on Windows).
_SHELLS_LOWER = frozenset(name.lower() for name in _SHELLS)
_FLAGGED_LOWER = frozenset(name.lower() for name in _FLAGGED_INTERPRETERS)
_UNCONDITIONAL_LOWER = frozenset(name.lower() for name in _UNCONDITIONAL)


def _exe_name(raw: str) -> str:
    """Normalize a command[0] token to a canonical executable name.

    Strips directories (both separators), drive prefixes, quoted forms, and
    the .exe suffix, then lowercases: 'C:\\Windows\\System32\\CMD.EXE',
    'sh', '"./PwSh"' all normalize to distinct denylist-comparable stems
    ('cmd', 'sh', 'pwsh').
    """
    token = raw.strip().strip('"').strip("'")
    token = token.replace("\\", "/")
    token = token.rsplit("/", 1)[-1]
    if re.match(r"^[A-Za-z]:", token):
        token = token[2:]
    name = token.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _code_flag_positions(command: Sequence[str]) -> list[int]:
    """Indices of flags that introduce a string of executable code."""
    positions: list[int] = []
    for index, part in enumerate(command[1:], start=1):
        if part in _CODE_FLAGS or part.lower() in {f.lower() for f in _CODE_FLAGS}:
            positions.append(index)
    return positions


def check_command(command: Sequence[str]) -> str | None:
    """Return a denial reason if the command violates the denylist, else None.

    Never raises. The caller decides how to surface the denial.
    """
    if not command:
        return None

    exe = _exe_name(command[0])

    # 1. Shells and eval-style constructs: blocked as command[0], regardless
    #    of case or path prefix.
    if exe in _SHELLS_LOWER or command[0].strip().lower() in _EVAL:
        return f"shell or eval-style interpreter '{command[0]}' is denied at the agent boundary"

    # 2. Unconditional interpreters (awk/sed/nc ...).
    if exe in _UNCONDITIONAL_LOWER:
        return f"interpreter '{command[0]}' is denied unconditionally at the agent boundary"

    # 3. Flagged interpreters: blocked when a code-string flag is present
    #    anywhere in the argument vector.
    if exe in _FLAGGED_LOWER:
        flags = _code_flag_positions(command)
        if flags:
            flagged = ", ".join(command[i] for i in flags)
            return (
                f"interpreter '{command[0]}' invoked with code-string flag(s) "
                f"({flagged}) is denied at the agent boundary"
            )

    # 4. File executors (sh/bash/powershell) invoked with a script file
    #    argument instead of -c: still a shell, still blocked by rule 1.
    #    Documented here to make the coverage explicit; rule 1 already
    #    denies them by name.
    return None


def is_denied(command: Sequence[str]) -> bool:
    """Boolean convenience wrapper over check_command."""
    return check_command(command) is not None
