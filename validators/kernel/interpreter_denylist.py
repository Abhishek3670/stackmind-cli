"""Hard interpreter denylist and command allowlist for the agent-facing command boundary.

Blocks invocation of shells and general-purpose interpreters capable of
executing arbitrary code from a single string argument (bash -c, python -c,
node -e, ...). For non-GitOps roles, strictly restricts git to read-only
subcommands and allowlisted flags, and denies publishing tools and wrappers.
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
# script file (python script.py) stays allowed (subject to P0-3 post-turn diff gate).
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

# Flags that take a string of code or execute arbitrary modules for the interpreters above.
_CODE_FLAGS = frozenset({
    "-c", "--command",       # python/perl/ruby/php/lua -c
    "-e",                    # perl/ruby/node -e
    "--eval", "-p", "-n",    # perl -p/-n one-liners; node --eval
    "-Command", "-EncodedCommand",
    "-m",                    # python -m arbitrary module execution
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

# Non-GitOps Git restrictions (P0-1)
_GIT_READ_ONLY_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "blame"})

_GIT_DENIED_FLAG_PREFIXES = (
    "-c",
    "-C",
    "--git-dir",
    "--work-tree",
    "--exec-path",
    "--output",
    "--ext-diff",
    "--textconv",
    "--config-env",
)

_GIT_ALLOWED_FLAGS = frozenset({
    "--no-optional-locks",
    "--oneline",
    "--stat",
    "--name-only",
    "--name-status",
    "-p",
    "--patch",
    "-s",
    "--short",
    "-b",
    "--branch",
    "--color",
    "--no-color",
    "-n",
    "--max-count",
    "-q",
    "--quiet",
    "--graph",
    "--decorate",
})

_PUBLISH_TOOLS_NON_GITOPS = frozenset({"gh", "hub"})

_GITOPS_ROLES = frozenset({"gitops", "local-llm", "release"})


def is_gitops_role(role: str | None) -> bool:
    if not role:
        return False
    norm = str(role).strip().lower()
    return norm in _GITOPS_ROLES


def _exe_name(raw: str) -> str:
    """Normalize a command[0] token to a canonical executable name."""
    token = raw.strip().strip('"').strip("'")
    token = token.replace("\\", "/")
    token = token.rsplit("/", 1)[-1]
    if re.match(r"^[A-Za-z]:", token):
        token = token[2:]
    name = token.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _unwrap_command(command: Sequence[str]) -> Sequence[str]:
    """Unwrap execution shims (env, command, xargs, find -exec, nice, sudo, etc.)."""
    if not command:
        return command

    exe = _exe_name(command[0])

    if exe in ("env", "command", "nice", "nohup", "sudo", "time", "timeout"):
        idx = 1
        while idx < len(command) and command[idx].startswith("-"):
            # skip option flags to wrappers (e.g. env -i, nice -n 10)
            idx += 1
        if idx < len(command):
            return _unwrap_command(command[idx:])

    if exe == "xargs":
        idx = 1
        while idx < len(command) and command[idx].startswith("-"):
            idx += 1
        if idx < len(command):
            return _unwrap_command(command[idx:])

    if exe == "find":
        for i, part in enumerate(command):
            if part in ("-exec", "-execdir") and i + 1 < len(command):
                inner = []
                for token in command[i + 1:]:
                    if token in (";", "+"):
                        break
                    if token != "{}":
                        inner.append(token)
                if inner:
                    return _unwrap_command(inner)

    return command


def _code_flag_positions(command: Sequence[str]) -> list[int]:
    """Indices of flags that introduce a string of executable code."""
    positions: list[int] = []
    for index, part in enumerate(command[1:], start=1):
        if part in _CODE_FLAGS or part.lower() in {f.lower() for f in _CODE_FLAGS}:
            positions.append(index)
    return positions


def check_command(command: Sequence[str], role: str | None = None) -> str | None:
    """Return a denial reason if the command violates policy, else None.

    Never raises. The caller decides how to surface the denial.
    """
    if not command:
        return None

    # Resolve wrappers before checking
    cmd = _unwrap_command(command)
    if not cmd:
        return None

    exe = _exe_name(cmd[0])
    is_gitops = is_gitops_role(role)

    # 1. Shells and eval-style constructs: blocked as command[0]
    if exe in _SHELLS_LOWER or cmd[0].strip().lower() in _EVAL:
        return f"shell or eval-style interpreter '{cmd[0]}' is denied at the agent boundary"

    # 2. Unconditional interpreters (awk/sed/nc ...)
    if exe in _UNCONDITIONAL_LOWER:
        return f"interpreter '{cmd[0]}' is denied unconditionally at the agent boundary"

    # 3. Flagged interpreters: blocked when a code-string/module flag is present
    if exe in _FLAGGED_LOWER:
        flags = _code_flag_positions(cmd)
        if flags:
            flagged = ", ".join(cmd[i] for i in flags)
            return (
                f"interpreter '{cmd[0]}' invoked with code-string flag(s) "
                f"({flagged}) is denied at the agent boundary"
            )

    # 4. Non-GitOps specific constraints
    if not is_gitops:
        # 4a. Publishing tools
        if exe in _PUBLISH_TOOLS_NON_GITOPS:
            return f"publishing tool '{exe}' is denied for non-GitOps roles (role: '{role}')"

        # 4b. Package managers publish/version subcommands
        if exe in ("npm", "pnpm", "yarn") and len(cmd) > 1:
            sub = cmd[1].lower()
            if sub in ("publish", "version", "pack"):
                return f"command '{exe} {sub}' is denied for non-GitOps roles (role: '{role}')"

        # 4c. Git command and flag allowlist
        if exe == "git":
            args = list(cmd[1:])
            # Check for denied flag prefixes anywhere in arguments
            for arg in args:
                for prefix in _GIT_DENIED_FLAG_PREFIXES:
                    if arg == prefix or arg.startswith(prefix + "=") or (prefix.startswith("-c") and arg.startswith("-c")):
                        return f"git flag '{arg}' is denied for non-GitOps roles (advisory allowlist P0-1)"

            # Find the subcommand (first positional token not starting with '-')
            subcmd = None
            for arg in args:
                if not arg.startswith("-"):
                    subcmd = arg.lower()
                    break

            if subcmd is None:
                # Bare 'git' or 'git --help' without a subcommand
                return "bare 'git' invocation without an allowlisted read-only subcommand is denied"

            if subcmd not in _GIT_READ_ONLY_SUBCOMMANDS:
                return (
                    f"git subcommand '{subcmd}' is denied for role '{role}': "
                    f"non-GitOps roles are limited to read-only git ({', '.join(sorted(_GIT_READ_ONLY_SUBCOMMANDS))})"
                )

    return None


def is_denied(command: Sequence[str], role: str | None = None) -> bool:
    """Boolean convenience wrapper over check_command."""
    return check_command(command, role=role) is not None
