"""Cross-platform clipboard access for StackMind TUI.

Provides native clipboard read and write operations across Windows (ctypes Win32 API),
macOS (pbpaste/pbcopy), and Linux (xclip/wl-clipboard/xsel), with zero external dependencies.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from typing import Optional


def get_clipboard_text() -> str:
    """Retrieve plain text from the system clipboard.

    Returns an empty string if clipboard is empty, unavailable, or inaccessible.
    """
    if os.name == "nt":
        return _get_clipboard_windows()
    elif sys.platform == "darwin":
        return _get_clipboard_macos()
    else:
        return _get_clipboard_linux()


def set_clipboard_text(text: str) -> bool:
    """Set plain text into the system clipboard.

    Returns True on success, False on failure.
    """
    if os.name == "nt":
        return _set_clipboard_windows(text)
    elif sys.platform == "darwin":
        return _set_clipboard_macos(text)
    else:
        return _set_clipboard_linux(text)


# ─── Windows Implementation (ctypes Win32 API) ───────────────────────────────

def _get_clipboard_windows() -> str:
    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = ctypes.c_void_p

        # Retry opening clipboard up to 5 times (handles contention)
        opened = False
        for _ in range(5):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.01)

        if not opened:
            return ""

        try:
            CF_UNICODETEXT = 13
            h_data = user32.GetClipboardData(CF_UNICODETEXT)
            if not h_data:
                return ""
            kernel32.GlobalLock.restype = ctypes.c_wchar_p
            ptr = kernel32.GlobalLock(h_data)
            text = str(ptr) if ptr else ""
            kernel32.GlobalUnlock(h_data)
            return text
        finally:
            user32.CloseClipboard()
    except Exception:
        return ""


def _set_clipboard_windows(text: str) -> bool:
    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

        opened = False
        for _ in range(5):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.01)

        if not opened:
            return False

        try:
            user32.EmptyClipboard()
            data = text.encode("utf-16le") + b"\x00\x00"
            GMEM_MOVEABLE = 0x0002
            h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h_mem:
                return False
            ptr = kernel32.GlobalLock(h_mem)
            if not ptr:
                return False
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(h_mem)
            CF_UNICODETEXT = 13
            res = user32.SetClipboardData(CF_UNICODETEXT, h_mem)
            return bool(res)
        finally:
            user32.CloseClipboard()
    except Exception:
        return False


# ─── macOS Implementation (pbpaste / pbcopy) ──────────────────────────────────

def _get_clipboard_macos() -> str:
    if shutil.which("pbpaste"):
        try:
            res = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=1.0)
            if res.returncode == 0:
                return res.stdout
        except Exception:
            pass
    return ""


def _set_clipboard_macos(text: str) -> bool:
    if shutil.which("pbcopy"):
        try:
            res = subprocess.run(["pbcopy"], input=text, text=True, timeout=1.0)
            return res.returncode == 0
        except Exception:
            pass
    return False


# ─── Linux Implementation (wl-paste / xclip / xsel) ───────────────────────────

def _get_clipboard_linux() -> str:
    # Wayland
    if shutil.which("wl-paste"):
        try:
            res = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=1.0)
            if res.returncode == 0:
                return res.stdout
        except Exception:
            pass

    # X11 xclip
    if shutil.which("xclip"):
        try:
            res = subprocess.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True, text=True, timeout=1.0)
            if res.returncode == 0:
                return res.stdout
        except Exception:
            pass

    # X11 xsel
    if shutil.which("xsel"):
        try:
            res = subprocess.run(["xsel", "--clipboard", "--output"], capture_output=True, text=True, timeout=1.0)
            if res.returncode == 0:
                return res.stdout
        except Exception:
            pass

    return ""


def _set_clipboard_linux(text: str) -> bool:
    # Wayland
    if shutil.which("wl-copy"):
        try:
            res = subprocess.run(["wl-copy"], input=text, text=True, timeout=1.0)
            return res.returncode == 0
        except Exception:
            pass

    # X11 xclip
    if shutil.which("xclip"):
        try:
            res = subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True, timeout=1.0)
            return res.returncode == 0
        except Exception:
            pass

    # X11 xsel
    if shutil.which("xsel"):
        try:
            res = subprocess.run(["xsel", "--clipboard", "--input"], input=text, text=True, timeout=1.0)
            return res.returncode == 0
        except Exception:
            pass

    return False
