from __future__ import annotations

import ctypes
import os
import platform
import sys

from .errors import NotAdminError, PlatformNotSupportedError


def is_windows() -> bool:
    return sys.platform == "win32"


def validate_supported_platform() -> None:
    if sys.platform not in {"win32", "linux", "darwin"}:
        raise PlatformNotSupportedError("py-ntfs-quick-index supports Windows, Linux, and macOS.")
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine not in {"amd64", "x86_64"}:
        raise PlatformNotSupportedError("py-ntfs-quick-index only supports amd64 CPUs.")


def is_admin() -> bool:
    if not is_windows():
        return os.geteuid() == 0 if hasattr(os, "geteuid") else True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def require_admin() -> None:
    if not is_windows():
        return
    if not is_admin():
        raise NotAdminError("Administrator privileges are required.")

