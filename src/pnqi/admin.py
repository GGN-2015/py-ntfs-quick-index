from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from .errors import NotAdminError
from .platform import is_admin, is_windows, validate_supported_platform

ELEVATED_CHILD_FLAG = "--pnqi-elevated-child"


def without_elevated_flag(argv: Sequence[str]) -> list[str]:
    return [arg for arg in argv if arg != ELEVATED_CHILD_FLAG]


def ensure_startup_admin(argv: Sequence[str] | None = None, *, gui: bool = False) -> bool:
    """Ensure the process is elevated once, at program startup.

    Returns True in the process that should continue. Returns False after
    requesting elevation from the original process.
    """

    validate_supported_platform()
    if not is_windows():
        return True
    args = list(sys.argv[1:] if argv is None else argv)
    if is_admin():
        return True
    if ELEVATED_CHILD_FLAG in args:
        raise NotAdminError("Elevation was requested but administrator rights are still missing.")

    from py_admin_launch import AdminLaunchError, launch

    child_args = [arg for arg in args if arg != ELEVATED_CHILD_FLAG]
    if gui and "--gui" not in child_args:
        child_args.insert(0, "--gui")
    if getattr(sys, "frozen", False):
        command = [sys.executable, ELEVATED_CHILD_FLAG, *child_args]
    else:
        command = [sys.executable, "-m", "pnqi", ELEVATED_CHILD_FLAG, *child_args]
    try:
        launch(command, cwd=os.getcwd(), wait=False)
    except (AdminLaunchError, OSError) as exc:
        raise NotAdminError(f"Could not launch administrator process: {exc}") from exc
    return False
