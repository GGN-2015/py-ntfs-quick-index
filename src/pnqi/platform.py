from __future__ import annotations

import ctypes
import platform
import sys

from .errors import NotAdminError, PlatformNotSupportedError


def validate_supported_platform() -> None:
    if sys.platform != "win32":
        raise PlatformNotSupportedError("py-ntfs-quick-index only supports Windows.")
    machine = platform.machine().lower()
    if machine not in {"amd64", "x86_64"}:
        raise PlatformNotSupportedError("py-ntfs-quick-index only supports amd64 CPUs.")


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def require_admin() -> None:
    if not is_admin():
        raise NotAdminError("Administrator privileges are required.")

