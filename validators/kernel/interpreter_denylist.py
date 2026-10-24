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
    "py", "py.exe",
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

# Allowlisted standard modules for python -m (P0-1)
# Blocks arbitrary script/server module execution while preserving test/package tooling.
_PYTHON_ALLOWED_MODULES = frozenset({
    "pytest",
    "pip",
    "venv",
    "unittest",
    "py_compile",
    "json.tool",
})

# Interpreters blocked regardless of flags: their scripting model is a string
# program (or they exist to spawn other shells).
_UNCONDITIONAL = frozenset({
    "awk", "gawk", "mawk", "sed",
    "nc", "netcat", "ncat", "socat",
    "start",
})

_EVAL = frozenset({"eval", "exec", "source", "."})

# Lowercased views for membership checks (exe names fold case on Windows).
_SHELLS_LOWER = frozenset(name.lower() for name in _SHELLS)
_FLAGGED_LOWER = frozenset(name.lower() for name in _FLAGGED_INTERPRETERS)
_UNCONDITIONAL_LOWER = frozenset(name.lower() for name in _UNCONDITIONAL)

# Non-GitOps Git restrictions (P0-1)
_GIT_READ_ONLY_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "blame"})

# Valueless global flags permitted before the subcommand for non-GitOps roles
_GIT_ALLOWED_GLOBAL_FLAGS = frozenset({"--no-pager", "--no-optional-locks"})

# Per-subcommand flag allowlists (for tokens starting with '-')
_GIT_SUBCOMMAND_ALLOWED_FLAGS: dict[str, frozenset[str]] = {
    "status": frozenset({
        "-s", "--short", "-b", "--branch", "--show-stash", "--ahead-behind",
        "--no-ahead-behind", "--porcelain", "--ignored", "--no-optional-locks",
        "-u", "--untracked-files", "-uno", "-unormal", "-uall",
    }),
    "diff": frozenset({
        "--stat", "--name-only", "--name-status", "-p", "--patch", "-s",
        "--no-patch", "--summary", "--color", "--no-color", "--word-diff",
        "--staged", "--cached", "--raw", "--no-ext-diff", "--no-textconv", "--no-index",
        "--no-optional-locks", "-U", "--unified", "-w", "--ignore-all-space",
        "-b", "--ignore-space-change", "--ignore-space-at-eol",
        "-M", "--find-renames", "-C", "--find-copies",
    }),
    "log": frozenset({
        "--oneline", "--stat", "--name-only", "--name-status", "-p", "--patch",
        "-s", "--no-patch", "--graph", "--decorate", "--color", "--no-color",
        "-c", "--cc", "--no-merges", "--merges", "--first-parent",
        "--topo-order", "--date-order", "--reverse", "-n", "--max-count",
        "--all", "--no-optional-locks", "--no-ext-diff", "--no-textconv",
    }),
    "show": frozenset({
        "--stat", "--name-only", "--name-status", "-p", "--patch", "-s",
        "--no-patch", "--color", "--no-color", "-c", "--cc", "--oneline",
        "--no-optional-locks", "--no-ext-diff", "--no-textconv",
    }),
    "blame": frozenset({
        "-c", "-C", "-M", "-w", "-s", "-p", "--porcelain", "--line-porcelain",
        "--color-lines", "--color-by-age", "--show-stats", "--show-name",
        "--show-number", "-l", "-t", "-L", "--score-debug", "--no-optional-locks",
    }),
}

# Prefix-matched flags specifically permitted with parameter values (e.g. -n5, -L1,10, --format=...)
_GIT_SUBCOMMAND_ALLOWED_PREFIXES: dict[str, tuple[str, ...]] = {
    "status": ("--porcelain=", "--untracked-files="),
    "diff": ("--diff-filter=", "-U", "--unified=", "--word-diff="),
    "log": ("-n", "--max-count=", "--format=", "--pretty=", "--date=", "--author=", "--grep="),
    "show": ("--format=", "--pretty="),
    "blame": ("-L", "--date="),
}

_PUBLISH_TOOLS_NON_GITOPS = frozenset({"gh", "hub"})

_GITOPS_ROLES = frozenset({"gitops", "release"})


def _is_python_exe(exe: str) -> bool:
    """Return True if the executable is a Python interpreter or launcher."""
    if exe in ("python", "py", "pypy"):
        return True
    if re.match(r"^python\d+(\.\d+)*$", exe):
        return True
    if re.match(r"^pypy\d+(\.\d+)*$", exe):
        return True
    return False


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

    import shlex
    exe = _exe_name(command[0])

    if exe in ("env", "command", "nice", "nohup", "sudo", "time", "timeout"):
        idx = 1
        while idx < len(command):
            arg = command[idx]
            if exe == "env":
                if arg in ("-S", "--split-string") and idx + 1 < len(command):
                    return _unwrap_command(shlex.split(command[idx + 1]))
                if arg in ("-u", "--unset") and idx + 1 < len(command):
                    idx += 2
                    continue
                if arg.startswith("-") or "=" in arg:
                    idx += 1
                    continue
                break
            elif exe in ("nice", "timeout", "sudo"):
                if arg.startswith("-"):
                    idx += 1
                    # Skip option flag argument (e.g. -n 10, -u root, -s 9)
                    if idx < len(command) and not command[idx].startswith("-"):
                        idx += 1
                    continue
                elif exe == "timeout" and (arg.isdigit() or (arg[:-1].isdigit() and arg[-1] in "smhd")):
                    idx += 1
                    continue
                break
            else:
                if arg.startswith("-"):
                    idx += 1
                    continue
                break
        if idx < len(command):
            sub = command[idx:]
            if len(sub) == 1 and " " in sub[0]:
                return _unwrap_command(shlex.split(sub[0]))
            return _unwrap_command(sub)

    if exe == "xargs":
        idx = 1
        while idx < len(command):
            arg = command[idx]
            if arg.startswith("-"):
                idx += 1
                if arg in ("-I", "-i", "-n", "-s", "-d", "-E") and idx < len(command):
                    idx += 1
                continue
            break
        if idx < len(command):
            sub = command[idx:]
            if len(sub) == 1 and " " in sub[0]:
                return _unwrap_command(shlex.split(sub[0]))
            return _unwrap_command(sub)

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
                    if len(inner) == 1 and " " in inner[0]:
                        return _unwrap_command(shlex.split(inner[0]))
                    return _unwrap_command(inner)

    if exe in ("cmd", "cmd.exe"):
        idx = 1
        while idx < len(command):
            arg = command[idx]
            if arg.lower() in ("/c", "/k", "-c"):
                idx += 1
                if idx < len(command):
                    sub = command[idx:]
                    if len(sub) == 1 and " " in sub[0]:
                        return _unwrap_command(shlex.split(sub[0]))
                    return _unwrap_command(sub)
            elif arg.startswith("/"):
                idx += 1
                continue
            break

    if exe == "start":
        idx = 1
        while idx < len(command):
            arg = command[idx]
            if arg.startswith("/") or arg.startswith("-"):
                idx += 1
                continue
            if arg.startswith('"') and arg.endswith('"') and idx + 1 < len(command):
                idx += 1
                continue
            break
        if idx < len(command):
            sub = command[idx:]
            if len(sub) == 1 and " " in sub[0]:
                return _unwrap_command(shlex.split(sub[0]))
            return _unwrap_command(sub)

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
    if _is_python_exe(exe):
        args = list(cmd[1:])
        if exe == "py":
            while args and (args[0].startswith("-2") or args[0].startswith("-3")):
                args.pop(0)
        for index, part in enumerate(args):
            if part in ("-c", "--command") or part.lower() in ("-command", "-encodedcommand"):
                return f"interpreter '{cmd[0]}' invoked with code-string flag '{part}' is denied at the agent boundary"
            if part == "-m":
                if index + 1 < len(args):
                    mod = args[index + 1].lower()
                    if mod not in _PYTHON_ALLOWED_MODULES:
                        return (
                            f"python -m module '{mod}' is not in allowlisted modules "
                            f"({', '.join(sorted(_PYTHON_ALLOWED_MODULES))})"
                        )
                else:
                    return "python -m without a module name is denied"
            elif part.startswith("-m"):
                mod = part[2:].lower()
                if mod not in _PYTHON_ALLOWED_MODULES:
                    return (
                        f"python -m module '{mod}' is not in allowlisted modules "
                        f"({', '.join(sorted(_PYTHON_ALLOWED_MODULES))})"
                    )

    elif exe in ("node", "node.exe") or exe.startswith("node"):
        for part in cmd[1:]:
            if part in ("-e", "--eval", "-p", "--print", "-r", "--require", "--loader", "--import"):
                return f"node execution flag '{part}' is denied at the agent boundary"
            if part.startswith("-r") or part.startswith("--require=") or part.startswith("--eval="):
                return f"node execution flag '{part}' is denied at the agent boundary"

    elif exe in _FLAGGED_LOWER:
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
            idx = 0
            # 1. Strict parse: only valueless global flags before subcommand
            while idx < len(args) and args[idx].startswith("-"):
                flag = args[idx]
                if flag not in _GIT_ALLOWED_GLOBAL_FLAGS:
                    return (
                        f"global git flag '{flag}' before subcommand is denied for non-GitOps roles: "
                        f"only valueless flags ({', '.join(sorted(_GIT_ALLOWED_GLOBAL_FLAGS))}) are allowed before subcommand"
                    )
                idx += 1

            if idx >= len(args):
                return "bare 'git' invocation without an allowlisted read-only subcommand is denied"

            subcmd = args[idx].lower()
            if subcmd not in _GIT_READ_ONLY_SUBCOMMANDS:
                return (
                    f"git subcommand '{subcmd}' is denied for role '{role}': "
                    f"non-GitOps roles are limited to read-only git ({', '.join(sorted(_GIT_READ_ONLY_SUBCOMMANDS))})"
                )

            # 2. Per-subcommand flag validation
            allowed_flags = _GIT_SUBCOMMAND_ALLOWED_FLAGS.get(subcmd, frozenset())
            allowed_prefixes = _GIT_SUBCOMMAND_ALLOWED_PREFIXES.get(subcmd, ())

            sub_args = args[idx + 1:]
            for arg in sub_args:
                if (
                    arg.startswith("--output")
                    or arg.startswith("--ext-diff")
                    or arg.startswith("--textconv")
                    or arg.startswith("--exec-path")
                    or arg.startswith("--git-dir")
                    or arg.startswith("--work-tree")
                    or arg.startswith("--config-env")
                ):
                    return f"git flag '{arg}' is denied for non-GitOps roles under '{subcmd}'"

                if arg.startswith("-"):
                    if arg == "--":
                        break
                    if arg in allowed_flags:
                        continue
                    if any(arg.startswith(prefix) for prefix in allowed_prefixes):
                        continue
                    if subcmd == "log" and re.match(r"^-\d+$", arg):
                        continue
                    return f"git flag '{arg}' is not in the allowlist for '{subcmd}' (role: '{role}')"

    return None


def is_denied(command: Sequence[str], role: str | None = None) -> bool:
    """Boolean convenience wrapper over check_command."""
    return check_command(command, role=role) is not None
