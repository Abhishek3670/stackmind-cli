"""
Process tree reaper for StackMind supervisor runtime (P0-2/P0-3).

Enforces mandatory termination of all child processes, descendant trees, and
background workers before post-turn diff analysis and working tree restore.
Prevents uncontained processes from racing with the restore engine, re-planting
symlinks, or holding file locks during workspace revert.

Host substrate:
- Windows (primary host): enumerates process hierarchies via Toolhelp32 snapshots
  and terminates descendant trees via kernel32.TerminateProcess.
- POSIX: terminates process groups via os.killpg(pid, signal.SIGKILL).
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

    def register_pid(self, pid: int) -> None:
        """Register a spawned process PID to be reaped on turn completion."""
        if pid > 0:
            self._registered_pids.add(pid)

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
            reaped = self._reap_posix(target_pids)

        self._registered_pids.difference_update(reaped)
        return reaped

    def _reap_windows(self, target_pids: set[int]) -> list[int]:
        """Windows process tree reaping using Win32 Toolhelp32 and TerminateProcess."""
        if not target_pids:
            return []

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
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
        kernel32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        # Snapshot active system processes
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == wintypes.HANDLE(-1).value or not snap:
            # Fallback to direct process termination
            return self._terminate_direct(target_pids)

        parent_map: dict[int, list[int]] = {}  # parent_pid -> list of child_pids
        existing_pids: set[int] = set()
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)

        try:
            if kernel32.Process32First(snap, ctypes.byref(pe)):
                while True:
                    pid = pe.th32ProcessID
                    ppid = pe.th32ParentProcessID
                    existing_pids.add(pid)
                    parent_map.setdefault(ppid, []).append(pid)
                    if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                        break
        finally:
            kernel32.CloseHandle(snap)

        # Build full descendant tree for each target PID
        all_to_kill: list[int] = []
        for root_pid in target_pids:
            if root_pid not in existing_pids:
                continue
            # BFS to find all descendants
            queue = [root_pid]
            visited = set()
            family = []
            while queue:
                curr = queue.pop(0)
                if curr in visited:
                    continue
                visited.add(curr)
                family.append(curr)
                for child in parent_map.get(curr, []):
                    if child not in visited:
                        queue.append(child)
            # Kill leaves first (reverse order)
            family.reverse()
            for p in family:
                if p not in all_to_kill and p != os.getpid():
                    all_to_kill.append(p)

        # Terminate processes
        reaped: list[int] = []
        for pid in all_to_kill:
            h = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
            if h:
                try:
                    kernel32.TerminateProcess(h, 1)
                    reaped.append(pid)
                finally:
                    kernel32.CloseHandle(h)

        return reaped

    def _terminate_direct(self, pids: set[int]) -> list[int]:
        """Direct termination fallback when snapshot is unavailable."""
        reaped = []
        for pid in pids:
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                reaped.append(pid)
            except (ProcessLookupError, OSError):
                pass
        return reaped

    def _reap_posix(self, target_pids: set[int]) -> list[int]:
        """POSIX process tree reaping via process groups and SIGKILL."""
        reaped = []
        for pid in target_pids:
            if pid <= 0 or pid == os.getpid():
                continue
            try:
                # Attempt to kill process group first
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
