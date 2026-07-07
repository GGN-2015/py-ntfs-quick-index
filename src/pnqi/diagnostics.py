from __future__ import annotations

import os
import platform
import sys
import tempfile
import time
from pathlib import Path

from . import __app_name__, __version__

LOG_DIR_ENV = "PNQI_LOG_DIR"


def log_dir() -> Path:
    override = os.environ.get(LOG_DIR_ENV)
    if override:
        return Path(override)
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / __app_name__ / "logs"


def write_error_log(
    *,
    error_type: str,
    message: str,
    traceback_text: str,
    context: dict[str, object] | None = None,
) -> str:
    directory = log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        directory = Path(tempfile.gettempdir()) / __app_name__ / "logs"
        directory.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"pnqi-error-{timestamp}-{os.getpid()}-{time.time_ns()}.log"
    path = directory / filename
    lines = [
        f"Application: {__app_name__} {__version__}",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"PID: {os.getpid()}",
        f"Python: {sys.version.replace(os.linesep, ' ')}",
        f"Platform: {platform.platform()}",
        f"Error type: {error_type}",
        f"Message: {message}",
    ]
    if context:
        lines.append("Context:")
        for key, value in sorted(context.items()):
            lines.append(f"  {key}: {value}")
    lines.extend(["", "Traceback:", traceback_text.rstrip() or "(no traceback captured)", ""])
    try:
        path.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        filename = f"pnqi-error-{timestamp}-{os.getpid()}-{time.time_ns()}.log"
        path = Path(tempfile.gettempdir()) / filename
        path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
