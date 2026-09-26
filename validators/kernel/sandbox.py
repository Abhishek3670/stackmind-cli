"""Contained process execution rooted in a ScratchWorkspace."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Mapping, Sequence

from .workspace import ScratchWorkspace, WorkspaceEscapeError

# Environment variables copied into sandboxed children. Everything else in the
# daemon environment (API keys, tokens, cloud credentials, service URLs) is
# dropped: a sandboxed command must never be able to read the parent's secrets
# via os.environ.
#
# Justification per entry:
# - PATH: executable resolution for bare commands (pytest, git, npm) and venv
#   continuation (.venv/Scripts is on the parent's PATH).
# - HOME / USERPROFILE / HOMEDRIVE / HOMEPATH: user config lookup for git/npm.
# - TEMP / TMP / TMPDIR: temp-file locations used by most toolchains.
# - LANG / LC_ALL / LANGUAGE: locale; some tools crash without them.
# - Windows runtime essentials: python.exe fails to initialize without
#   SystemRoot; npm/git/MSVC tooling expects SYSTEMDRIVE, WINDIR, COMSPEC,
#   PATHEXT, APPDATA, LOCALAPPDATA, PROGRAMFILES, ALLUSERSPROFILE, PUBLIC.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LANGUAGE",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "ALLUSERSPROFILE",
    "PUBLIC",
)

# Resource limit defaults (Phase 3).
# Memory ceiling: 512 MB prevents runaway memory bombs while allowing normal toolchains.
DEFAULT_MEMORY_LIMIT_BYTES: int = 512 * 1024 * 1024
# CPU time limit: 60s CPU execution time prevents runaway loops from spinning cores indefinitely.
DEFAULT_CPU_LIMIT_SECONDS: float = 60.0

_SENTINEL = object()

# POSIX limits are applied by a tiny compiled C shim that is exec'd BEFORE the
# real command: it applies setrlimit() and then execvp()s the actual argv. This
# keeps every post-fork/pre-exec operation a libc syscall sequence with no
# Python code running between fork and exec, which eliminates the documented
# preexec_fn deadlock class in multi-threaded parents (CPython runs
# PyObject_Call in the forked child for preexec_fn, holding a forked copy of
# every lock another parent thread held at fork time; it also disables vfork
# when preexec_fn is set). The shim's own syscalls (getenv/setrlimit/execvp)
# are async-signal-safe and do not malloc or take locks in glibc.
_SHIM_SOURCE = r'''#include <sys/resource.h>
#include <sys/types.h>
#include <stdlib.h>
#include <unistd.h>
int main(int argc, char **argv) {
    const char *mem = getenv("STACKMIND_RLIMIT_BYTES");
    const char *cpu = getenv("STACKMIND_RLIMIT_CPU");
    struct rlimit rl;
    if (mem) {
        rl.rlim_cur = rl.rlim_max = (rlim_t)strtoull(mem, 0, 10);
        setrlimit(RLIMIT_AS, &rl);
        setrlimit(RLIMIT_DATA, &rl);
    }
    if (cpu) {
        int secs = atoi(cpu);
        rl.rlim_cur = (rlim_t)secs;
        rl.rlim_max = (rlim_t)(secs + 1);
        setrlimit(RLIMIT_CPU, &rl);
    }
    if (argc < 2) return 125;
    execvp(argv[1], &argv[1]);
    return 127;
}
'''
_SHIM_PATH = Path(tempfile.gettempdir()) / "stackmind-rlimit-shim"


def _ensure_posix_shim() -> str | None:
    """Compile (once) and return the path of the setrlimit exec shim.

    Returns None when the shim cannot be built or on non-POSIX platforms; the
    caller then falls back to running the command directly without limits
    (graceful degradation, same policy as the previous implementation).
    """
    if sys.platform == "win32":
        return None
    source_path = _SHIM_PATH.with_suffix(".c")
    binary = str(_SHIM_PATH)
    try:
        if _SHIM_PATH.exists():
            # Trust the cached binary ONLY if the on-disk source is untouched:
            # recompile whenever the source mtime is newer than the binary's,
            # so a stale/modified source cannot silently keep an old binary.
            if source_path.exists() and _SHIM_PATH.stat().st_mtime >= source_path.stat().st_mtime:
                return binary
        source_path.write_text(_SHIM_SOURCE, encoding="utf-8")
        subprocess.run(
            ["cc", "-O2", "-o", binary, str(source_path)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return binary
    except Exception:
        return None


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryLimit", ctypes.c_size_t),
            ("PeakJobMemoryLimit", ctypes.c_size_t),
        ]

    _JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    _JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectExtendedLimitInformation = 9
    _CREATE_SUSPENDED = 0x00000004


def _setup_windows_job(
    memory_limit_bytes: int | None,
    cpu_time_limit_seconds: float | None,
) -> int | None:
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limit_flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        if memory_limit_bytes is not None and memory_limit_bytes > 0:
            limit_flags |= (_JOB_OBJECT_LIMIT_PROCESS_MEMORY | _JOB_OBJECT_LIMIT_JOB_MEMORY)
            info.ProcessMemoryLimit = memory_limit_bytes
            info.JobMemoryLimit = memory_limit_bytes

        if cpu_time_limit_seconds is not None and cpu_time_limit_seconds > 0:
            limit_flags |= _JOB_OBJECT_LIMIT_PROCESS_TIME
            # Win32 time limit is specified in 100-nanosecond ticks (1s = 10,000,000 ticks)
            info.BasicLimitInformation.PerProcessUserTimeLimit = int(
                cpu_time_limit_seconds * 10_000_000
            )

        info.BasicLimitInformation.LimitFlags = limit_flags
        success = kernel32.SetInformationJobObject(
            job,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not success:
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _resume_windows_main_thread(kernel32, pid: int) -> bool:
    """Resume the initial (main) thread of a CREATE_SUSPENDED child.

    CPython's Popen does not expose the child's thread handle, so locate it
    via the toolhelp snapshot: the first thread owned by the child process is
    the one CREATE_SUSPENDED paused.
    """
    TH32CS_SNAPTHREAD = 0x00000004
    THREAD_SUSPEND_RESUME = 0x0002

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snapshot:
        return False
    entry = THREADENTRY32()
    entry.dwSize = ctypes.sizeof(THREADENTRY32)
    thread_id = None
    listed = kernel32.Thread32First(snapshot, ctypes.byref(entry))
    while listed:
        if entry.th32OwnerProcessID == pid:
            thread_id = entry.th32ThreadID
            break
        listed = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    kernel32.CloseHandle(snapshot)
    if thread_id is None:
        return False
    thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, thread_id)
    if not thread:
        return False
    try:
        return kernel32.ResumeThread(thread) != 0xFFFFFFFF  # (DWORD)-1 == error
    finally:
        kernel32.CloseHandle(thread)


def _assign_windows_job(job: int | None, proc: subprocess.Popen) -> bool:
    """Assign a SUSPENDED child to the job, then resume its main thread.

    The child is created with CREATE_SUSPENDED by the caller, so assignment
    happens strictly before any user instruction executes: there is no window
    in which the process can allocate memory or burn CPU outside the limits.

    Returns True only if the child was attached AND its main thread resumed.
    On any failure the caller must kill the child and fail closed: an
    unresumed child would hang, an unattached child would run without limits.
    """
    if sys.platform != "win32" or not job:
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if not kernel32.AssignProcessToJobObject(job, int(proc._handle)):
            return False
        return _resume_windows_main_thread(kernel32, proc.pid)
    except Exception:
        return False


def _close_windows_job(job: int | None) -> None:
    if sys.platform != "win32" or not job:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(job)
    except Exception:
        pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def is_resource_limit_failure(self) -> bool:
        """Check whether the command execution failed due to memory or CPU limits."""
        if "MemoryError" in self.stderr or "out of memory" in self.stderr.lower():
            return True
        if self.returncode in (3221225540, -1073741756, 0xC0000044):
            return True
        if self.returncode in (3221225495, -1073741801, 0xC0000017):
            return True
        if self.returncode in (-24, 128 + 24):
            return True
        return False


class ProcessSandbox:
    def __init__(
        self,
        workspace: ScratchWorkspace,
        *,
        env_allowlist: Sequence[str] | None = None,
        memory_limit_bytes: int | None = DEFAULT_MEMORY_LIMIT_BYTES,
        cpu_time_limit_seconds: float | None = DEFAULT_CPU_LIMIT_SECONDS,
    ) -> None:
        self.workspace = workspace
        self.env_allowlist = tuple(
            env_allowlist if env_allowlist is not None else DEFAULT_ENV_ALLOWLIST
        )
        self.memory_limit_bytes = memory_limit_bytes
        self.cpu_time_limit_seconds = cpu_time_limit_seconds

    def _child_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """Build the scrubbed child environment.

        Only allowlisted variables are inherited from the parent; Windows env
        names are case-insensitive, so matching is done case-insensitively and
        the parent's original casing is preserved. Explicit extras are merged
        last and win over inherited values.
        """
        allowed = {name.upper() for name in self.env_allowlist}
        env: dict[str, str] = {}
        seen: set[str] = set()
        for key, value in os.environ.items():
            marker = key.upper()
            if marker in allowed and marker not in seen:
                env[key] = value
                seen.add(marker)
        for key, value in (extra or {}).items():
            env[key] = value
        return env

    @staticmethod
    def _validate(command: Sequence[str]) -> None:
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("Commands must be a non-empty string sequence")
        for argument in command[1:]:
            path = PurePath(argument.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts:
                raise WorkspaceEscapeError("Command contains an out-of-workspace path")

    def run(
        self,
        command: Sequence[str],
        timeout: float = 30,
        *,
        env_extra: Mapping[str, str] | None = None,
        memory_limit_bytes: int | None | object = _SENTINEL,
        cpu_time_limit_seconds: float | None | object = _SENTINEL,
    ) -> CommandResult:
        self._validate(command)

        mem_limit = (
            self.memory_limit_bytes
            if memory_limit_bytes is _SENTINEL
            else memory_limit_bytes
        )
        cpu_limit = (
            self.cpu_time_limit_seconds
            if cpu_time_limit_seconds is _SENTINEL
            else cpu_time_limit_seconds
        )

        job = None
        shim: str | None = None
        if sys.platform == "win32":
            job = _setup_windows_job(mem_limit, cpu_limit)
        else:
            if (mem_limit is not None and mem_limit > 0) or (
                cpu_limit is not None and cpu_limit > 0
            ):
                shim = _ensure_posix_shim()

        # Limits travel to the shim via the environment; the child's own env
        # is rebuilt from the allowlist, so these variables are injected into
        # that already-scrubbed environment (they are not inherited secrets).
        limit_env = self._child_env(env_extra)
        if shim is not None:
            if mem_limit is not None and mem_limit > 0:
                limit_env["STACKMIND_RLIMIT_BYTES"] = str(int(mem_limit))
            if cpu_limit is not None and cpu_limit > 0:
                limit_env["STACKMIND_RLIMIT_CPU"] = str(int(max(1, cpu_limit)))

        argv = [shim, *command] if shim is not None else list(command)

        # Windows: create the child SUSPENDED so the Job Object (with its
        # memory/CPU limits) is attached before the first instruction runs.
        # POSIX: start_new_session gives the child its own process group
        # (setsid in libc, no Python post-fork hook) so the timeout path can
        # kill the whole tree via killpg.
        creationflags = _CREATE_SUSPENDED if (sys.platform == "win32" and job is not None) else 0

        proc = None
        try:
            proc = subprocess.Popen(
                argv,
                cwd=self.workspace.root,
                env=limit_env,
                shell=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=(sys.platform != "win32"),
                creationflags=creationflags,
            )
            if job is not None and not _assign_windows_job(job, proc):
                # Fail closed: never execute a command whose limits could not
                # be attached before its first instruction.
                proc.kill()
                raise RuntimeError(
                    "sandbox: could not attach child to Job Object; limits "
                    "cannot be guaranteed, command refused"
                )

            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                if sys.platform != "win32":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
                stdout, stderr = proc.communicate()
                raise

            returncode = proc.poll() if proc.poll() is not None else 0
            return CommandResult(returncode, stdout, stderr)
        finally:
            if job is not None:
                _close_windows_job(job)

