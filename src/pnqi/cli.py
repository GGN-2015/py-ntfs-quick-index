from __future__ import annotations

import argparse
import ntpath
import sys
from collections.abc import Callable
from typing import Any

from tqdm import tqdm

from .admin import ELEVATED_CHILD_FLAG, ensure_startup_admin, without_elevated_flag
from .errors import OperationCancelled, PnqiError
from .formatting import human_mtime, human_percent, human_size
from .indexer import browse_children, build_index, list_sizes, refresh_known_indexes, search, update_index
from .pathing import normalize_for_match, normalize_windows_path
from .progress import CancellationToken, ProgressUpdate
from .winapi import logical_drive_roots


class TqdmProgress:
    def __init__(self) -> None:
        self._bars: dict[str, tqdm] = {}

    def close(self) -> None:
        for bar in self._bars.values():
            bar.close()
        self._bars.clear()

    def __call__(self, update: ProgressUpdate) -> None:
        total = update.total
        current = update.current
        key = update.stage
        bar = self._bars.get(key)
        if bar is None:
            bar = tqdm(
                total=total,
                desc=key,
                unit="item",
                dynamic_ncols=True,
                leave=key == "done",
            )
            self._bars[key] = bar
        elif total is not None and bar.total != total:
            bar.total = total
            bar.refresh()
        if current is not None:
            delta = current - int(bar.n)
            if delta > 0:
                bar.update(delta)
            elif delta < 0:
                bar.n = current
                bar.refresh()
        if update.message:
            bar.set_postfix_str(update.message[:80])
        if update.stage == "done":
            bar.close()


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--skip-startup-refresh",
        action="store_true",
        help="do not refresh existing pnqi.index.sqlite files at startup",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pnqi",
        description="Fast NTFS index and search tool for Windows amd64.",
    )
    parser.add_argument("--gui", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(ELEVATED_CHILD_FLAG, nargs="?", help=argparse.SUPPRESS)
    _add_common(parser)
    subparsers = parser.add_subparsers(dest="command")

    drives_parser = subparsers.add_parser("drives", help="list local drive roots available on this computer")
    drives_parser.add_argument("--ntfs-only", action="store_true", help="show only drives accepted as NTFS")

    index_parser = subparsers.add_parser("index", help="create or replace an index for a folder")
    index_parser.add_argument("path", help="folder to index")

    refresh_parser = subparsers.add_parser("refresh", help="refresh an existing index for a path or drive")
    refresh_parser.add_argument("path", help="indexed path or drive to refresh")

    search_parser = subparsers.add_parser("search", help="search indexed files by a * wildcard path")
    search_parser.add_argument("pattern", help="path pattern; * matches any string including backslashes")
    search_parser.add_argument("--limit", type=int, default=0, help="maximum number of rows to print")
    search_parser.add_argument(
        "--drive",
        help="drive root used to resolve relative patterns, for example C:\\",
    )
    search_parser.add_argument("--details", action="store_true", help="show type and modified time columns")

    browse_parser = subparsers.add_parser(
        "browse",
        help="show direct children of an indexed folder like the GUI folder browser",
    )
    browse_parser.add_argument("path", help="folder to browse")
    browse_parser.add_argument("--limit", type=int, default=0, help="maximum number of rows to print")

    sizes_parser = subparsers.add_parser(
        "sizes",
        help="show files and folders sorted by recursive size descending",
    )
    sizes_parser.add_argument("path", help="folder to inspect")
    sizes_parser.add_argument(
        "--direct",
        action="store_true",
        help="show only direct children instead of all descendants",
    )
    sizes_parser.add_argument("--limit", type=int, default=0, help="maximum number of rows to print")
    sizes_parser.add_argument("--details", action="store_true", help="show type and modified time columns")
    return parser


def _refresh_startup(args: argparse.Namespace, progress: Callable[[ProgressUpdate], None], token: CancellationToken) -> None:
    if args.skip_startup_refresh or args.command == "drives":
        return
    refresh_known_indexes(progress=progress, token=token)


def _entry_size(entry: Any) -> int:
    return int(entry.tree_size if entry.is_dir else entry.size)


def _entry_kind(entry: Any) -> str:
    return "Folder" if entry.is_dir else "File"


def _print_entries(entries: list[Any], *, details: bool = False) -> None:
    if details:
        print(f"{'Size':>12}  {'Type':<6}  {'Modified':<19}  Path")
    for entry in entries:
        size = human_size(_entry_size(entry))
        if details:
            print(f"{size:>12}  {_entry_kind(entry):<6}  {human_mtime(entry.mtime_ns):<19}  {entry.path}")
        else:
            print(f"{size:>12}  {entry.path}")


def _print_browse(root: Any, entries: list[Any], *, limit: int) -> None:
    shown = entries if limit <= 0 else entries[:limit]
    total_size = _entry_size(root)
    print(f"{'Size':>12}  {'Share':>8}  {'Type':<6}  {'Modified':<19}  Path")
    for entry in shown:
        size_bytes = _entry_size(entry)
        print(
            f"{human_size(size_bytes):>12}  "
            f"{human_percent(size_bytes, total_size):>8}  "
            f"{_entry_kind(entry):<6}  "
            f"{human_mtime(entry.mtime_ns):<19}  "
            f"{entry.path}"
        )
    if limit > 0 and len(entries) > limit:
        print(f"... {len(entries) - limit} more")


def _is_inside_root(root: str, path: str) -> bool:
    root_norm = normalize_for_match(root).rstrip("\\") + "\\"
    candidate = normalize_for_match(path).rstrip("\\") + "\\"
    return candidate.startswith(root_norm)


def _pattern_in_drive(drive: str, pattern: str) -> str:
    root = normalize_windows_path(drive)
    pattern = pattern.replace("/", "\\")
    drive_name, _tail = ntpath.splitdrive(pattern)
    if drive_name:
        resolved = normalize_windows_path(pattern)
    else:
        resolved = normalize_windows_path(root + pattern.lstrip("\\"))
    if not _is_inside_root(root, resolved):
        raise PnqiError("Search patterns must stay inside the selected drive.")
    return resolved


def _validate_limit(limit: int) -> None:
    if limit < 0:
        raise PnqiError("Limit must be a non-negative integer.")


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(without_elevated_flag(raw_args))
    if args.gui:
        from .gui import main as gui_main

        return gui_main(without_elevated_flag(raw_args))

    if args.command is None:
        parser.print_help()
        return 2

    if not ensure_startup_admin(raw_args):
        print("Requested administrator privileges; the elevated pnqi process will continue.")
        return 0

    token = CancellationToken()
    progress = TqdmProgress()
    try:
        _refresh_startup(args, progress, token)
        if args.command == "drives":
            drives = logical_drive_roots()
            if args.ntfs_only:
                from .winapi import get_volume_info

                ntfs_drives = []
                for drive in drives:
                    try:
                        get_volume_info(drive)
                    except PnqiError:
                        continue
                    ntfs_drives.append(drive)
                drives = ntfs_drives
            for drive in drives:
                print(drive)
        elif args.command == "index":
            index_path = build_index(args.path, progress=progress, token=token)
            print(f"Index written: {index_path}")
        elif args.command == "refresh":
            index_path = update_index(args.path, progress=progress, token=token)
            print(f"Index refreshed: {index_path}")
        elif args.command == "search":
            _validate_limit(args.limit)
            pattern = _pattern_in_drive(args.drive, args.pattern) if args.drive else args.pattern
            rows = search(pattern, limit=args.limit, progress=progress, token=token)
            _print_entries(rows, details=args.details)
        elif args.command == "browse":
            _validate_limit(args.limit)
            root, rows = browse_children(args.path, progress=progress, token=token)
            _print_browse(root, rows, limit=args.limit)
        elif args.command == "sizes":
            _validate_limit(args.limit)
            rows = list_sizes(args.path, recursive=not args.direct, progress=progress, token=token)
            shown = rows if args.limit <= 0 else rows[: args.limit]
            _print_entries(shown, details=args.details)
            if args.limit > 0 and len(rows) > args.limit:
                print(f"... {len(rows) - args.limit} more")
        else:
            parser.error(f"unknown command: {args.command}")
        return 0
    except KeyboardInterrupt:
        token.cancel()
        print("", file=sys.stderr)
        print("Cancelled.", file=sys.stderr)
        return 130
    except OperationCancelled:
        print("Cancelled.", file=sys.stderr)
        return 130
    except PnqiError as exc:
        print(f"pnqi: {exc}", file=sys.stderr)
        return 1
    finally:
        progress.close()

def main(argv: list[str] | None = None) -> int:
    return run(argv)

