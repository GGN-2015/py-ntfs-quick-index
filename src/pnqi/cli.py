from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from tqdm import tqdm

from .admin import ELEVATED_CHILD_FLAG, ensure_startup_admin, without_elevated_flag
from .errors import OperationCancelled, PnqiError
from .formatting import human_size
from .indexer import build_index, list_sizes, refresh_known_indexes, search
from .progress import CancellationToken, ProgressUpdate


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

    index_parser = subparsers.add_parser("index", help="create or replace an index for a folder")
    index_parser.add_argument("path", help="folder to index")

    search_parser = subparsers.add_parser("search", help="search indexed files by a * wildcard path")
    search_parser.add_argument("pattern", help="path pattern; * matches any string including backslashes")
    search_parser.add_argument("--limit", type=int, default=0, help="maximum number of rows to print")

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
    return parser


def _refresh_startup(args: argparse.Namespace, progress: Callable[[ProgressUpdate], None], token: CancellationToken) -> None:
    if args.skip_startup_refresh:
        return
    refresh_known_indexes(progress=progress, token=token)


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
        if args.command == "index":
            index_path = build_index(args.path, progress=progress, token=token)
            print(f"Index written: {index_path}")
        elif args.command == "search":
            rows = search(args.pattern, progress=progress, token=token)
            shown = rows if args.limit <= 0 else rows[: args.limit]
            for entry in shown:
                print(f"{entry.display_size:>12}  {entry.path}")
            if args.limit > 0 and len(rows) > args.limit:
                print(f"... {len(rows) - args.limit} more")
        elif args.command == "sizes":
            rows = list_sizes(args.path, recursive=not args.direct, progress=progress, token=token)
            shown = rows if args.limit <= 0 else rows[: args.limit]
            for entry in shown:
                print(f"{human_size(entry.tree_size if entry.is_dir else entry.size):>12}  {entry.path}")
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

