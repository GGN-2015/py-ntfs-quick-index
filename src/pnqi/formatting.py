from __future__ import annotations

from datetime import datetime


UNITS = ("B", "KB", "MB", "GB", "TB", "PB", "EB")


def human_size(size: int) -> str:
    value = float(max(size, 0))
    unit_index = 0
    while value >= 1000.0 and unit_index < len(UNITS) - 1:
        value /= 1000.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} B"
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    if len(text.split(".", 1)[0]) > 3 and unit_index < len(UNITS) - 1:
        value /= 1000.0
        unit_index += 1
        text = f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{text} {UNITS[unit_index]}"


def human_percent(part: int, total: int) -> str:
    if total <= 0:
        return "0%"
    value = max(part, 0) / total * 100.0
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{text}%"


def human_mtime(mtime_ns: int) -> str:
    if mtime_ns <= 0:
        return ""
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S")
