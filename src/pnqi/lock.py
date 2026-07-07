from __future__ import annotations

import ctypes
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import __app_name__
from .progress import CancellationToken, ProgressCallback, ProgressUpdate, report

LOCK_WAIT_MESSAGE = "Waiting for filesystem index file lock to be released"
STATE_DIR_ENV = "PNQI_STATE_DIR"
LOCK_STALE_AFTER_SECONDS = 60 * 60


def _state_dir() -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / __app_name__


def lock_paths() -> tuple[Path, Path]:
    state_dir = _state_dir()
    return state_dir / "index.lock", state_dir / "index.status.json"


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    process_query_limited_information = 0x1000
    still_active = 259
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() != error_invalid_parameter
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return int(exit_code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        loaded = json.loads(data)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp_path, path)


def _lock_age_seconds(path: Path) -> float:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return 0.0


def _remove_lock_files(lock_path: Path, status_path: Path) -> None:
    for path in (lock_path, status_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _lock_is_stale(lock_path: Path, status_path: Path) -> bool:
    owner = _read_json(status_path) or _read_json(lock_path)
    pid = owner.get("pid")
    if isinstance(pid, int):
        return not _process_is_running(pid)
    if isinstance(pid, str) and pid.isdigit():
        return not _process_is_running(int(pid))
    return _lock_age_seconds(lock_path) >= LOCK_STALE_AFTER_SECONDS


class IndexWorkLock:
    def __init__(self, operation: str, target: str) -> None:
        self.operation = operation
        self.target = target
        self.lock_path, self.status_path = lock_paths()
        self._fd: int | None = None

    def acquire(
        self,
        *,
        progress: ProgressCallback | None,
        token: CancellationToken | None,
    ) -> None:
        last_reported = 0.0
        while True:
            if token is not None:
                token.check()
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if _lock_is_stale(self.lock_path, self.status_path):
                    _remove_lock_files(self.lock_path, self.status_path)
                    continue
                now = time.monotonic()
                if now - last_reported >= 1.0:
                    report(progress, ProgressUpdate("lock", 0, None, LOCK_WAIT_MESSAGE))
                    last_reported = now
                time.sleep(0.2)
                continue

            payload = {
                "pid": os.getpid(),
                "operation": self.operation,
                "target": self.target,
                "started_at_ns": time.time_ns(),
                "updated_at_ns": time.time_ns(),
            }
            os.write(self._fd, json.dumps(payload, sort_keys=True).encode("utf-8"))
            os.fsync(self._fd)
            _write_json(self.status_path, payload)
            return

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        _remove_lock_files(self.lock_path, self.status_path)


@contextmanager
def acquire_index_lock(
    operation: str,
    target: str,
    *,
    progress: ProgressCallback | None = None,
    token: CancellationToken | None = None,
) -> Iterator[None]:
    lock = IndexWorkLock(operation, target)
    lock.acquire(progress=progress, token=token)
    try:
        yield
    finally:
        lock.release()
