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


def get_clipboard_image() -> tuple[bytes, str] | None:
    """Retrieve raw image bytes and file extension (e.g., (bytes, 'png')) from system clipboard.

    Returns None if no image is present on the clipboard.
    """
    if os.name == "nt":
        return _get_clipboard_image_windows()
    elif sys.platform == "darwin":
        return _get_clipboard_image_macos()
    else:
        return _get_clipboard_image_linux()


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


def _dib_to_bmp(dib_bytes: bytes) -> bytes:
    """Prepend a standard 14-byte BITMAPFILEHEADER to raw DIB / DIBV5 bytes."""
    import struct

    if len(dib_bytes) < 40:
        return b""
    header_size = struct.unpack_from("<I", dib_bytes, 0)[0]
    bit_count = struct.unpack_from("<H", dib_bytes, 14)[0]
    compression = struct.unpack_from("<I", dib_bytes, 16)[0]
    clr_used = struct.unpack_from("<I", dib_bytes, 32)[0] if header_size >= 36 else 0

    if clr_used == 0 and bit_count <= 8:
        palette_count = 1 << bit_count
    else:
        palette_count = clr_used

    extra_masks = 12 if (compression == 3 and header_size == 40) else 0
    bf_off_bits = 14 + header_size + extra_masks + (palette_count * 4)
    file_header = struct.pack("<2sIHHI", b"BM", 14 + len(dib_bytes), 0, 0, bf_off_bits)
    return file_header + dib_bytes


def _get_clipboard_image_windows() -> tuple[bytes, str] | None:
    """Retrieve raw image bytes and format extension from Windows clipboard."""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_bool
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = ctypes.c_bool
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
        user32.IsClipboardFormatAvailable.restype = ctypes.c_bool
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_bool
        kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.restype = ctypes.c_size_t

        opened = False
        for _ in range(5):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.01)

        if not opened:
            return _get_clipboard_image_windows_powershell()

        try:
            # 1. First check registered PNG format (standard for modern browsers, discord, snipping tool)
            png_fmt = user32.RegisterClipboardFormatW("PNG")
            if png_fmt and user32.IsClipboardFormatAvailable(png_fmt):
                h_data = user32.GetClipboardData(png_fmt)
                if h_data:
                    size = kernel32.GlobalSize(h_data)
                    ptr = kernel32.GlobalLock(h_data)
                    if ptr and size > 0:
                        raw = ctypes.string_at(ptr, size)
                        kernel32.GlobalUnlock(h_data)
                        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                            return (raw, "png")

            # 2. Check CF_DIB (8) or CF_DIBV5 (17) (universal on Windows for screenshots, printscreen)
            for dib_id in (8, 17):
                if user32.IsClipboardFormatAvailable(dib_id):
                    h_data = user32.GetClipboardData(dib_id)
                    if h_data:
                        size = kernel32.GlobalSize(h_data)
                        ptr = kernel32.GlobalLock(h_data)
                        if ptr and size > 0:
                            raw_dib = ctypes.string_at(ptr, size)
                            kernel32.GlobalUnlock(h_data)
                            bmp = _dib_to_bmp(raw_dib)
                            if bmp:
                                return (bmp, "bmp")
        finally:
            user32.CloseClipboard()
    except Exception:
        pass

    return _get_clipboard_image_windows_powershell()


def _get_clipboard_image_windows_powershell() -> tuple[bytes, str] | None:
    """PowerShell STA fallback to convert any clipboard image into PNG bytes."""
    try:
        import base64

        ps_cmd = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$img = [System.Windows.Forms.Clipboard]::GetImage(); "
            "if ($img) { "
            "$ms = New-Object System.IO.MemoryStream; "
            "$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png); "
            "[Convert]::ToBase64String($ms.ToArray()) "
            "}"
        )
        res = subprocess.run(
            ["powershell", "-Sta", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            data = base64.b64decode(res.stdout.strip())
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                return (data, "png")
    except Exception:
        pass
    return None


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


def _get_clipboard_image_macos() -> tuple[bytes, str] | None:
    if shutil.which("pngpaste"):
        try:
            res = subprocess.run(["pngpaste", "-"], capture_output=True, timeout=1.5)
            if res.returncode == 0 and res.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
                return (res.stdout, "png")
        except Exception:
            pass
    return None


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


def _get_clipboard_image_linux() -> tuple[bytes, str] | None:
    if shutil.which("wl-paste"):
        try:
            res = subprocess.run(["wl-paste", "-t", "image/png"], capture_output=True, timeout=1.5)
            if res.returncode == 0 and res.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
                return (res.stdout, "png")
        except Exception:
            pass
    if shutil.which("xclip"):
        try:
            res = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                capture_output=True,
                timeout=1.5,
            )
            if res.returncode == 0 and res.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
                return (res.stdout, "png")
        except Exception:
            pass
    return None
