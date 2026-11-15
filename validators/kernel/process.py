"""Governed background process lifecycle management for agent runtime."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .interpreter_denylist import check_command
from .sandbox import (
    ProcessSandbox,
    WorkspaceEscapeError,
    _setup_windows_job,
    _assign_windows_job,
    _ensure_posix_shim,
    _CREATE_SUSPENDED,
)
from .workspace import ScratchWorkspace


class ProcessError(RuntimeError):
    """Raised when process operations fail."""
    pass


def compute_task_timeout(command: Sequence[str], default_timeout: float | None = None) -> float:
    """Compute dynamic timeout budget: 300s for builds/package managers, 30s for inspection."""
    if default_timeout is not None and default_timeout > 0 and default_timeout != 30.0:
        return float(default_timeout)
    cmd0 = str(command[0]).lower() if command else ""
    cmd_joined = " ".join(str(c).lower() for c in command)
    long_running_bins = {
        "npm", "pnpm", "yarn", "npx", "pip", "pip3", "cargo",
        "pytest", "vitest", "playwright", "tsc", "dotnet", "mvn", "gradle",
    }
    long_running_subcmds = {
        "install", "update", "add", "build", "compile", "test",
        "generate", "migrate", "db:generate", "seed", "ci",
    }
    bin_name = Path(cmd0).stem.lower()
    if bin_name in long_running_bins or any(sub in cmd_joined for sub in long_running_subcmds):
        return 300.0
    return 30.0


@dataclass
class ManagedProcess:
    process_id: str
    command: list[str]
    proc: subprocess.Popen
    started_at: float
    timeout_budget: float = 300.0
    output_buffer: deque[str] = field(default_factory=lambda: deque(maxlen=2000))
    reader_thread: threading.Thread | None = None
    _stopped: bool = False
    _timed_out: bool = False
    on_complete: Any = None

    @property
    def is_alive(self) -> bool:
        if self._stopped or self._timed_out:
            return False
        alive = self.proc.poll() is None
        if alive and (time.time() - self.started_at) > self.timeout_budget:
            self._timed_out = True
            try:
                self.proc.kill()
            except Exception:
                pass
            return False
        return alive

    @property
    def exit_code(self) -> int | None:
        if self._timed_out:
            return -1
        return self.proc.poll()

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.started_at

    def status(self) -> dict[str, Any]:
        alive = self.is_alive
        if self._timed_out:
            status_str = "timed_out"
        elif alive:
            status_str = "running"
        elif self._stopped:
            status_str = "stopped"
        elif self.exit_code == 0:
            status_str = "completed"
        else:
            status_str = "failed"
        return {
            "task_id": self.process_id,
            "process_id": self.process_id,
            "pid": self.proc.pid,
            "status": status_str,
            "exit_code": self.exit_code,
            "uptime_seconds": round(self.uptime_seconds, 2),
            "timeout_budget_seconds": self.timeout_budget,
            "remaining_seconds": max(0.0, round(self.timeout_budget - self.uptime_seconds, 2)),
            "command": list(self.command),
        }

    def get_output(self, tail_lines: int = 100) -> str:
        lines = list(self.output_buffer)
        if tail_lines > 0 and len(lines) > tail_lines:
            lines = lines[-tail_lines:]
        return "".join(lines)


class ProcessManager:
    """Manages background processes spawned within a ScratchWorkspace."""

    def __init__(self, workspace: ScratchWorkspace, sandbox: ProcessSandbox | None = None) -> None:
        self.workspace = workspace
        self.sandbox = sandbox or ProcessSandbox(workspace)
        self._processes: dict[str, ManagedProcess] = {}
        self._counter: int = 0
        self._lock = threading.Lock()

    def start_process(
        self,
        command: Sequence[str],
        env_extra: Mapping[str, str] | None = None,
        timeout: float | None = None,
        on_complete: Any = None,
    ) -> str:
        """Start a managed background process with dynamic timeout tiering.
        
        Enforces workspace confinement and interpreter denylist.
        """
        cmd_list = list(command)
        self.sandbox._validate(cmd_list)

        denial_reason = check_command(cmd_list)
        if denial_reason is not None:
            raise PermissionError(denial_reason)

        # Windows: resolve bare executable through PATH/PATHEXT (e.g. npm -> npm.cmd)
        if sys.platform == "win32" and cmd_list:
            exe0 = cmd_list[0]
            has_ext = os.path.splitext(exe0)[1].lower() in (".exe", ".cmd", ".bat", ".com")
            if not (os.path.isabs(exe0) and has_ext):
                resolved = shutil.which(exe0)
                if resolved:
                    cmd_list = [resolved] + list(cmd_list[1:])

        with self._lock:
            self._counter += 1
            proc_id = f"proc-{self._counter}"

        mem_limit = self.sandbox.memory_limit_bytes
        cpu_limit = self.sandbox.cpu_time_limit_seconds

        job = None
        shim: str | None = None
        if sys.platform == "win32":
            job = _setup_windows_job(mem_limit, cpu_limit)
        else:
            if (mem_limit is not None and mem_limit > 0) or (
                cpu_limit is not None and cpu_limit > 0
            ):
                shim = _ensure_posix_shim()

        limit_env = self.sandbox._child_env(env_extra)
        if shim is not None:
            if mem_limit is not None and mem_limit > 0:
                limit_env["STACKMIND_RLIMIT_BYTES"] = str(int(mem_limit))
            if cpu_limit is not None and cpu_limit > 0:
                limit_env["STACKMIND_RLIMIT_CPU"] = str(int(max(1, cpu_limit)))

        argv = [shim, *cmd_list] if shim is not None else cmd_list
        creationflags = _CREATE_SUSPENDED if (sys.platform == "win32" and job is not None) else 0

        # Start process with pipes for stdout and stderr combined
        proc = subprocess.Popen(
            argv,
            cwd=self.workspace.root,
            env=limit_env,
            shell=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,  # Line buffered
            creationflags=creationflags,
            start_new_session=(sys.platform != "win32"),
        )

        if sys.platform == "win32" and job is not None:
            if not _assign_windows_job(job, proc):
                try:
                    proc.kill()
                except Exception:
                    pass
                raise ProcessError("Failed to attach background process to Windows job object limits")

        timeout_budget = compute_task_timeout(cmd_list, timeout)
        managed = ManagedProcess(
            process_id=proc_id,
            command=cmd_list,
            proc=proc,
            started_at=time.time(),
            timeout_budget=timeout_budget,
            on_complete=on_complete,
        )

        def _drain_output():
            try:
                if proc.stdout is not None:
                    for line in iter(proc.stdout.readline, ""):
                        managed.output_buffer.append(line)
            except Exception:
                pass
            finally:
                if proc.stdout:
                    proc.stdout.close()
                if managed.on_complete:
                    try:
                        managed.on_complete(managed)
                    except Exception:
                        pass

        thread = threading.Thread(target=_drain_output, daemon=True)
        managed.reader_thread = thread
        thread.start()

        with self._lock:
            self._processes[proc_id] = managed

        return proc_id

    def get_status(self, process_id: str) -> dict[str, Any]:
        """Get status of a managed process."""
        with self._lock:
            proc = self._processes.get(process_id)
        if proc is None:
            raise ProcessError(f"Process '{process_id}' not found")
        return proc.status()

    def get_output(self, process_id: str, tail_lines: int = 100) -> str:
        """Get the latest output lines from a managed process."""
        with self._lock:
            proc = self._processes.get(process_id)
        if proc is None:
            raise ProcessError(f"Process '{process_id}' not found")
        return proc.get_output(tail_lines)

    def stop_process(self, process_id: str, timeout: float = 5.0) -> bool:
        """Stop a running managed process."""
        with self._lock:
            proc = self._processes.get(process_id)
        if proc is None:
            raise ProcessError(f"Process '{process_id}' not found")

        if not proc.is_alive:
            proc._stopped = True
            return True

        proc._stopped = True
        try:
            proc.proc.terminate()
            proc.proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, Exception):
            try:
                proc.proc.kill()
                proc.proc.wait(timeout=2.0)
            except Exception:
                pass

        return not proc.is_alive

    def stop_all(self) -> None:
        """Terminate all managed processes (turn cleanup guarantee)."""
        with self._lock:
            procs = list(self._processes.values())
        for proc in procs:
            if proc.is_alive:
                proc._stopped = True
                try:
                    proc.proc.terminate()
                    proc.proc.wait(timeout=1.0)
                except Exception:
                    try:
                        proc.proc.kill()
                    except Exception:
                        pass

    cleanup_all = stop_all
