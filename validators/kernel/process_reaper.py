"""
Process tree reaper for StackMind supervisor runtime (P0-2/P0-3).

Enforces mandatory termination of all child processes, descendant trees, and
background workers before post-turn diff analysis and working tree restore.
Prevents uncontained processes from racing with the restore engine, re-planting
symlinks, or holding file locks during workspace revert.

Host substrate:
- Primary host: Windows 11 (tested, empirical Job Object containment).
- Job Object containment: assigns children to a Windows Job Object configured with
  JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and NO breakaway. When intermediate processes
  exit, grandchildren remain pinned in the job and are terminated on reap_all().
- POSIX portable mode: UNVERIFIED on POSIX (marked per project policy).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence


class ProcessReaper:
    """Terminates process trees before diff gating and snapshot restore."""

    def __init__(self) -> None:
        self._registered_pids: set[int] = set()
        self._registered_managers: list[Any] = []
        self._job_handle: int | None = None
        if sys.platform == "win32":
            self._init_job()

    def _init_job(self) -> None:
        """Create and configure a Windows Job Object with KILL_ON_JOB_CLOSE and no breakaway."""
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        _JobObjectExtendedLimitInformation = 9

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

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        # Note: JOB_OBJECT_LIMIT_BREAKAWAY_OK is omitted, so children cannot breakaway
        success = kernel32.SetInformationJobObject(
            job,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if success:
            self._job_handle = job
        else:
            kernel32.CloseHandle(job)
            self._job_handle = None

    def register_process(self, proc: subprocess.Popen) -> None:
        """Assign a launched process to the supervisor Job Object."""
        self.register_pid(proc.pid)
        if sys.platform == "win32" and self._job_handle and hasattr(proc, "_handle") and proc._handle:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.AssignProcessToJobObject(self._job_handle, int(proc._handle))

    def register_pid(self, pid: int) -> None:
        """Register a spawned process PID to be reaped on turn completion."""
        if pid > 0:
            self._registered_pids.add(pid)
            if sys.platform == "win32" and self._job_handle:
                import ctypes
                from ctypes import wintypes
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                PROCESS_SET_QUOTA = 0x0100
                PROCESS_TERMINATE = 0x0001
                h = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
                if h:
                    try:
                        kernel32.AssignProcessToJobObject(self._job_handle, h)
                    finally:
                        kernel32.CloseHandle(h)

    def register_manager(self, manager: Any) -> None:
        """Register a ProcessManager instance whose managed processes must be reaped."""
        if manager not in self._registered_managers:
            self._registered_managers.append(manager)

    def clear(self) -> None:
        """Clear registered PIDs and managers."""
        self._registered_pids.clear()
        self._registered_managers.clear()

    def reap_all(self, extra_pids: Sequence[int] | None = None) -> list[int]:
        """Reap all registered and extra process trees. Returns list of reaped PIDs."""
        # 1. Stop any registered ProcessManager instances
        for pm in self._registered_managers:
            try:
                if hasattr(pm, "stop_all"):
                    pm.stop_all()
                elif hasattr(pm, "cleanup_all"):
                    pm.cleanup_all()
            except Exception:
                pass

        # 2. Collect all target PIDs
        target_pids = set(self._registered_pids)
        if extra_pids:
            target_pids.update(p for p in extra_pids if p > 0)

        reaped: list[int] = []
        if sys.platform == "win32":
            reaped = self._reap_windows(target_pids)
        else:
            # POSIX killpg implementation is marked UNVERIFIED (tested host is Windows 11)
            reaped = self._reap_posix_unverified(target_pids)

        self._registered_pids.difference_update(reaped)
        return reaped

    def _reap_windows(self, target_pids: set[int]) -> list[int]:
        """Windows process tree reaping using Job Object termination with Toolhelp fallback."""
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        reaped: list[int] = list(target_pids)

        # 1. Primary containment: terminate Job Object
        # This instantaneously kills all processes and orphaned grandchildren in the job
        if self._job_handle:
            kernel32.TerminateJobObject(self._job_handle, 1)
            kernel32.CloseHandle(self._job_handle)
            self._job_handle = None
            # Recreate Job Object for subsequent operations
            self._init_job()

        # 2. Secondary fallback: direct Toolhelp cleanup pass
        self._fallback_toolhelp_cleanup(target_pids)

        return reaped

    def _fallback_toolhelp_cleanup(self, target_pids: set[int]) -> None:
        """Fallback toolhelp sweep in case any process failed to attach to the job."""
        if not target_pids:
            return

        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        PROCESS_TERMINATE = 0x0001
        SYNCHRONIZE = 0x00100000

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == wintypes.HANDLE(-1).value or not snap:
            return

        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        try:
            if kernel32.Process32First(snap, ctypes.byref(pe)):
                while True:
                    pid = pe.th32ProcessID
                    if pid in target_pids and pid != os.getpid():
                        h = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
                        if h:
                            try:
                                kernel32.TerminateProcess(h, 1)
                            finally:
                                kernel32.CloseHandle(h)
                    if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                        break
        finally:
            kernel32.CloseHandle(snap)

    def _reap_posix_unverified(self, target_pids: set[int]) -> list[int]:
        """POSIX process tree reaping via process groups (UNVERIFIED: Windows is the only tested substrate)."""
        reaped = []
        for pid in target_pids:
            if pid <= 0 or pid == os.getpid():
                continue
            try:
                pgid = os.getpgid(pid)
                if pgid > 1:
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
                reaped.append(pid)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGKILL)
                    reaped.append(pid)
                except OSError:
                    pass
        return reaped


# Global supervisor reaper instance
_GLOBAL_REAPER = ProcessReaper()


def get_global_reaper() -> ProcessReaper:
    """Retrieve the global supervisor ProcessReaper singleton."""
    return _GLOBAL_REAPER


def reap_process_tree(pids: Sequence[int] | None = None) -> list[int]:
    """Convenience function to reap all registered and specified process trees."""
    return _GLOBAL_REAPER.reap_all(extra_pids=pids)
