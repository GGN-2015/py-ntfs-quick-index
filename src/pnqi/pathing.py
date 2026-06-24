from __future__ import annotations

import ntpath
import os
from pathlib import Path

from . import INDEX_FILENAME


def normalize_windows_path(path: str) -> str:
    path = path.replace("/", "\\")
    normed = ntpath.normpath(path)
    drive, tail = ntpath.splitdrive(normed)
    if drive and tail in ("\\", ""):
        return drive.upper() + "\\"
    return normed.rstrip("\\")


def normalize_for_match(path: str) -> str:
    return normalize_windows_path(path).casefold()


def absolute_existing_path(path: str) -> str:
    return normalize_windows_path(str(Path(path).resolve(strict=True)))


def absolute_pattern(pattern: str) -> str:
    pattern = pattern.replace("/", "\\")
    drive, _ = ntpath.splitdrive(pattern)
    if drive:
        return normalize_windows_path(pattern)
    return normalize_windows_path(ntpath.abspath(pattern))


def join_windows_path(parent: str, name: str) -> str:
    if parent.endswith("\\"):
        return parent + name
    return parent + "\\" + name


def sqlite_like_from_star_pattern(pattern: str) -> str:
    normalized = normalize_for_match(absolute_pattern(pattern))
    out: list[str] = []
    for char in normalized:
        if char == "*":
            out.append("%")
        elif char in ("%", "_", "\\"):
            out.append("\\" + char)
        else:
            out.append(char)
    return "".join(out)


def index_path_for_volume(volume_root: str) -> str:
    return join_windows_path(normalize_windows_path(volume_root), INDEX_FILENAME)


def temporary_index_path(final_path: str, pid: int | None = None) -> str:
    suffix = os.getpid() if pid is None else pid
    return f"{final_path}.tmp.{suffix}"


def is_index_artifact(path: str, volume_root: str) -> bool:
    parent = normalize_for_match(ntpath.dirname(path))
    root = normalize_for_match(volume_root).rstrip("\\")
    name = ntpath.basename(path).casefold()
    return parent == root and name.startswith(INDEX_FILENAME.casefold())

