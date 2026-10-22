"""Durable, crash-safe I/O primitives for StackMind.

Prevents file corruption from power loss, unexpected process termination,
or OS pagefile exhaustion (such as NTFS unwritten cluster zero-filling).
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import yaml


def is_corrupted_or_zero_filled(path: Path | str) -> bool:
    """Check if a file exists and is corrupted with null bytes or zero-filled clusters.
    
    On Windows NTFS and Unix systems with delayed allocation, an abrupt crash
    can leave a file with an allocated size in directory metadata, but whose
    contents read back entirely as null bytes (\\x00).
    """
    p = Path(path)
    if not p.is_file():
        return False
    try:
        size = p.stat().st_size
        if size == 0:
            return True
        # Read the first 4KB to check for null-byte corruption
        with p.open("rb") as handle:
            chunk = handle.read(min(size, 4096))
            if not chunk or chunk == b"\x00" * len(chunk):
                return True
            if b"\x00" in chunk:
                return True
        return False
    except OSError:
        return True


def durable_write_text(
    path: Path | str,
    content: str,
    encoding: str = "utf-8",
    newline: str | None = None,
    max_retries: int = 5,
) -> None:
    """Write text to a file using atomic rename and fsync to prevent crash corruption.
    
    Steps:
    1. Writes to a temporary file in the same parent directory (.tmp.<pid>.<time>).
    2. Flushes Python stream buffers.
    3. Forces OS kernel cache to physical disk via os.fsync().
    4. Atomically replaces target file via os.replace().
    5. Cleans up temporary file if any step fails.
    """
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    
    # Use a hidden temp file in the same directory to guarantee same filesystem/volume
    temp_file = target.parent / f".{target.name}.tmp.{os.getpid()}.{time.time_ns()}"
    
    try:
        with temp_file.open("w", encoding=encoding, newline=newline) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            
        for attempt in range(max_retries):
            try:
                os.replace(temp_file, target)
                break
            except (PermissionError, OSError) as exc:
                if attempt == max_retries - 1:
                    # Fallback for aggressive Windows file locking
                    try:
                        with target.open("w", encoding=encoding, newline=newline) as handle:
                            handle.write(content)
                            handle.flush()
                            os.fsync(handle.fileno())
                    except Exception:
                        raise exc
                time.sleep(0.04 * (attempt + 1))
    finally:
        try:
            if temp_file.exists():
                temp_file.unlink()
        except OSError:
            pass


def durable_write_yaml(
    path: Path | str,
    data: Any,
    sort_keys: bool = False,
    default_flow_style: bool = False,
    allow_unicode: bool = True,
) -> None:
    """Serialize data to YAML and write it atomically with fsync."""
    yaml_content = yaml.dump(
        data,
        sort_keys=sort_keys,
        default_flow_style=default_flow_style,
        allow_unicode=allow_unicode,
    )
    durable_write_text(path, yaml_content, encoding="utf-8")


def clean_orphaned_tmp_files(directory: Path | str, max_age_seconds: float = 30.0) -> list[Path]:
    """Scan a directory recursively and remove orphaned .tmp.* files from past crashes."""
    dir_path = Path(directory)
    if not dir_path.is_dir():
        return []
    
    cleaned: list[Path] = []
    now = time.time()
    
    try:
        for item in dir_path.rglob("*.tmp.*"):
            if not item.is_file():
                continue
            try:
                # If older than max_age_seconds or belongs to non-running PID
                mtime = item.stat().st_mtime
                if (now - mtime) > max_age_seconds:
                    item.unlink(missing_ok=True)
                    cleaned.append(item)
                else:
                    # Check PID in filename
                    match = re.search(r"\.tmp\.(\d+)\.", item.name)
                    if match:
                        pid = int(match.group(1))
                        if not _is_pid_alive(pid):
                            item.unlink(missing_ok=True)
                            cleaned.append(item)
            except OSError:
                pass
    except OSError:
        pass
        
    return cleaned


def _is_pid_alive(pid: int) -> bool:
    """Check if process with given PID is currently active."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            STILL_ACTIVE = 259
            kernel32.CloseHandle(handle)
            return exit_code.value == STILL_ACTIVE
        kernel32.CloseHandle(handle)
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
