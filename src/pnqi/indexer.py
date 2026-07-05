from __future__ import annotations

import ntpath
import os
import string
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import __app_name__, __version__
from .db import (
    Entry,
    bulk_insert_entries,
    cancellable_query,
    connect,
    count_entries,
    create_schema,
    delete_subtree,
    entry_by_frn,
    entry_by_path,
    recompute_tree_sizes,
    refresh_descendant_paths,
    require_index,
    row_to_entry,
    set_metadata,
    stamp_finished_metadata,
    update_ancestor_sizes,
    upsert_entry,
    validate_index,
)
from .errors import IndexInvalidError, IndexNotFoundError, PnqiError
from .pathing import (
    absolute_existing_path,
    absolute_pattern,
    index_path_for_volume,
    is_index_artifact,
    join_windows_path,
    normalize_for_match,
    normalize_windows_path,
    sqlite_like_from_star_pattern,
    temporary_index_path,
)
from .platform import require_admin, validate_supported_platform
from .progress import CancellationToken, ProgressCallback, ProgressUpdate, report
from .winapi import (
    FILE_ATTRIBUTE_DIRECTORY,
    UsnRecord,
    enum_usn_records,
    get_file_id,
    get_volume_info,
    open_volume,
    query_usn_journal,
    read_usn_changes,
)

USN_REASON_FILE_CREATE = 0x00000100
USN_REASON_FILE_DELETE = 0x00000200
USN_REASON_RENAME_OLD_NAME = 0x00001000
USN_REASON_RENAME_NEW_NAME = 0x00002000
TREE_SIZE_VERSION = "2"


@dataclass
class _MftRecord:
    frn: str
    parent_frn: str
    name: str
    attributes: int
    usn: int
    timestamp: int


@dataclass
class _Change:
    frn: str
    flags: int
    first_order: int
    last_record: UsnRecord
    new_record: UsnRecord | None = None
    delete_record: UsnRecord | None = None


@dataclass(frozen=True)
class StagedIndex:
    final_path: str
    temp_path: str
    entry_count: int = 0


def _frn(value: int | str) -> str:
    return str(value)


def _is_dir(attributes: int) -> bool:
    return bool(attributes & FILE_ATTRIBUTE_DIRECTORY)


def _path_depth(path: str) -> int:
    stripped = normalize_windows_path(path).rstrip("\\")
    if not stripped:
        return 0
    return stripped.count("\\")


def _is_under(path: str, root: str) -> bool:
    path_norm = normalize_for_match(path)
    root_norm = normalize_for_match(root)
    if root_norm.endswith("\\"):
        return path_norm.startswith(root_norm)
    return path_norm == root_norm or path_norm.startswith(root_norm + "\\")


def _display_name_for_root(root_path: str) -> str:
    root_path = normalize_windows_path(root_path)
    if root_path.endswith("\\") and len(root_path) == 3:
        return root_path
    return ntpath.basename(root_path) or root_path


def _entry_sort_key(entry: Entry) -> tuple[int, int, int, str]:
    return (int(entry.usn), int(entry.mtime_ns), int(entry.is_dir), entry.frn)


def _entry_matches_current_file_id(entry: Entry) -> bool:
    try:
        file_id = get_file_id(entry.path)
    except (OSError, PnqiError):
        return False
    return _frn(file_id.frn) == entry.frn


def _choose_duplicate_path_entry(candidates: list[Entry], root_frn: str) -> Entry:
    for entry in candidates:
        if entry.frn == root_frn:
            return entry
    live_matches = [entry for entry in candidates if _entry_matches_current_file_id(entry)]
    if live_matches:
        return max(live_matches, key=_entry_sort_key)
    return max(candidates, key=_entry_sort_key)


def _keep_reachable_entries(entries: dict[str, Entry], root_frn: str) -> dict[str, Entry]:
    reachable: dict[str, Entry] = {}
    visiting: set[str] = set()

    def is_reachable(frn: str) -> bool:
        if frn in reachable:
            return True
        if frn in visiting:
            return False
        entry = entries.get(frn)
        if entry is None:
            return False
        if frn == root_frn:
            reachable[frn] = entry
            return True
        visiting.add(frn)
        parent = entries.get(entry.parent_frn)
        if parent is not None and parent.is_dir and is_reachable(parent.frn):
            reachable[frn] = entry
            visiting.remove(frn)
            return True
        visiting.remove(frn)
        return False

    for frn in list(entries):
        is_reachable(frn)
    return reachable


def _deduplicate_entries_by_path(entries: dict[str, Entry], root_frn: str) -> dict[str, Entry]:
    by_path: dict[str, list[Entry]] = {}
    for entry in entries.values():
        by_path.setdefault(normalize_for_match(entry.path), []).append(entry)
    if all(len(candidates) == 1 for candidates in by_path.values()):
        return entries

    selected: dict[str, Entry] = {}
    for candidates in by_path.values():
        chosen = candidates[0] if len(candidates) == 1 else _choose_duplicate_path_entry(candidates, root_frn)
        selected[chosen.frn] = chosen
    return _keep_reachable_entries(selected, root_frn)


def _accumulate_entry_tree_sizes(entries: dict[str, Entry], root_frn: str) -> None:
    for entry in sorted(entries.values(), key=lambda item: _path_depth(item.path), reverse=True):
        current = entries[entry.frn]
        if current.frn == root_frn:
            continue
        parent = entries.get(current.parent_frn)
        if parent is not None and parent.is_dir:
            entries[parent.frn] = Entry(
                frn=parent.frn,
                parent_frn=parent.parent_frn,
                name=parent.name,
                path=parent.path,
                is_dir=parent.is_dir,
                size=parent.size,
                tree_size=parent.tree_size + current.tree_size,
                mtime_ns=parent.mtime_ns,
                attributes=parent.attributes,
                usn=parent.usn,
            )


def _repair_index_tree_sizes_if_needed(
    conn,
    meta: dict[str, str],
    *,
    progress: ProgressCallback | None,
    token: CancellationToken,
) -> None:
    if meta.get("tree_size_version") == TREE_SIZE_VERSION:
        return
    report(progress, ProgressUpdate("size-recalc", 0, None, "Recalculating recursive folder sizes"))
    recompute_tree_sizes(conn, progress=progress, token=token)
    set_metadata(conn, tree_size_version=TREE_SIZE_VERSION)


def _stat_entry(
    path: str,
    *,
    frn: str,
    parent_frn: str,
    name: str,
    attributes: int,
    usn: int,
) -> Entry | None:
    try:
        stat_result = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    is_dir = _is_dir(attributes) or bool(stat_result.st_file_attributes & FILE_ATTRIBUTE_DIRECTORY)
    size = 0 if is_dir else int(stat_result.st_size)
    return Entry(
        frn=frn,
        parent_frn=parent_frn,
        name=name,
        path=normalize_windows_path(path),
        is_dir=is_dir,
        size=size,
        tree_size=size,
        mtime_ns=int(stat_result.st_mtime_ns),
        attributes=int(getattr(stat_result, "st_file_attributes", attributes)),
        usn=int(usn),
    )


def _volume_for_pattern(pattern: str):
    absolute = absolute_pattern(pattern)
    drive, _tail = ntpath.splitdrive(absolute)
    if not drive:
        raise PnqiError(f"Cannot determine volume for pattern: {pattern}")
    return get_volume_info(drive + "\\")


def _resolve_index_path_for_existing_path(path: str) -> tuple[str, str, object]:
    root_path = absolute_existing_path(path)
    volume = get_volume_info(root_path)
    return root_path, index_path_for_volume(volume.root), volume


def _replace_index(temp_path: str, final_path: str) -> None:
    if not os.path.exists(temp_path):
        raise PnqiError(f"Temporary index file was not created: {temp_path}")
    try:
        for suffix in ("", "-journal", "-wal", "-shm"):
            final_artifact = final_path + suffix
            temp_artifact = temp_path + suffix
            if suffix and os.path.exists(final_artifact):
                try:
                    os.remove(final_artifact)
                except OSError:
                    pass
            if suffix and os.path.exists(temp_artifact):
                try:
                    os.remove(temp_artifact)
                except OSError:
                    pass
        os.replace(temp_path, final_path)
    except OSError as exc:
        raise PnqiError(f"Could not write index file {final_path}: {exc}") from exc


def _cleanup_temp_index(temp_path: str) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.remove(temp_path + suffix)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def planned_staged_index(root_path: str) -> StagedIndex:
    validate_supported_platform()
    require_admin()
    _root_path, final_path, _volume = _resolve_index_path_for_existing_path(root_path)
    return StagedIndex(final_path=final_path, temp_path=temporary_index_path(final_path))


def commit_staged_index(staged: StagedIndex) -> str:
    _replace_index(staged.temp_path, staged.final_path)
    return staged.final_path


def discard_staged_index(staged: StagedIndex) -> None:
    _cleanup_temp_index(staged.temp_path)


def _build_index(
    root_path: str,
    *,
    progress: ProgressCallback | None = None,
    token: CancellationToken | None = None,
    commit: bool,
    staged: StagedIndex | None = None,
) -> str | StagedIndex:
    validate_supported_platform()
    require_admin()
    token = token or CancellationToken()
    root_path, final_path, volume = _resolve_index_path_for_existing_path(root_path)
    root_id = get_file_id(root_path)
    volume_root_id = get_file_id(volume.root)
    if root_id.volume_serial != volume.serial:
        raise PnqiError(f"{root_path} is not on {volume.root}.")

    if staged is not None:
        if normalize_for_match(staged.final_path) != normalize_for_match(final_path):
            raise PnqiError("Staged index target does not match the requested folder.")
        temp_path = staged.temp_path
    else:
        temp_path = temporary_index_path(final_path)
    _cleanup_temp_index(temp_path)
    report(progress, ProgressUpdate("start", 0, None, f"Index file: {final_path}"))

    try:
        with open_volume(volume) as handle:
            journal = query_usn_journal(handle)
            high_usn = journal.next_usn
            records: dict[str, _MftRecord] = {}
            report(progress, ProgressUpdate("mft", 0, None, "Reading NTFS MFT records"))
            for idx, record in enumerate(enum_usn_records(handle, high_usn), start=1):
                token.check()
                records[_frn(record.frn)] = _MftRecord(
                    frn=_frn(record.frn),
                    parent_frn=_frn(record.parent_frn),
                    name=record.name,
                    attributes=record.attributes,
                    usn=record.usn,
                    timestamp=record.timestamp,
                )
                if idx % 10000 == 0:
                    report(progress, ProgressUpdate("mft", idx, None, f"Read {idx:,} MFT records"))

        volume_root_frn = _frn(volume_root_id.frn)
        root_frn = _frn(root_id.frn)
        path_cache: dict[str, str | None] = {volume_root_frn: volume.root}

        def path_for(frn: str) -> str | None:
            if frn in path_cache:
                return path_cache[frn]
            record = records.get(frn)
            if record is None:
                path_cache[frn] = None
                return None
            parent_path = path_for(record.parent_frn)
            if parent_path is None:
                path_cache[frn] = None
                return None
            path_cache[frn] = join_windows_path(parent_path, record.name)
            return path_cache[frn]

        entries: dict[str, Entry] = {}
        root_record = records.get(root_frn)
        root_attributes = root_record.attributes if root_record else root_id.attributes
        root_usn = root_record.usn if root_record else high_usn
        root_parent = root_record.parent_frn if root_record else root_frn
        root_entry = _stat_entry(
            root_path,
            frn=root_frn,
            parent_frn=root_parent if root_frn != volume_root_frn else root_frn,
            name=_display_name_for_root(root_path),
            attributes=root_attributes,
            usn=root_usn,
        )
        if root_entry is None or not root_entry.is_dir:
            raise PnqiError(f"Cannot index {root_path}; it is not an accessible directory.")
        entries[root_frn] = root_entry

        report(progress, ProgressUpdate("stat", 0, len(records), "Collecting file sizes"))
        considered = 0
        for record in records.values():
            token.check()
            considered += 1
            if record.frn == root_frn:
                continue
            path = path_for(record.frn)
            if path is None or not _is_under(path, root_path):
                continue
            if is_index_artifact(path, volume.root):
                continue
            entry = _stat_entry(
                path,
                frn=record.frn,
                parent_frn=record.parent_frn,
                name=record.name,
                attributes=record.attributes,
                usn=record.usn,
            )
            if entry is not None:
                entries[entry.frn] = entry
            if considered % 5000 == 0:
                report(
                    progress,
                    ProgressUpdate("stat", considered, len(records), f"Collected {len(entries):,} entries"),
                )

        entries = _deduplicate_entries_by_path(entries, root_frn)
        _accumulate_entry_tree_sizes(entries, root_frn)

        conn = connect(temp_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            create_schema(conn)
            set_metadata(
                conn,
                app_name=__app_name__,
                app_version=__version__,
                schema_version="1",
                volume_root=volume.root,
                volume_serial=volume.serial,
                filesystem=volume.filesystem,
                root_path=root_path,
                root_frn=root_frn,
                journal_id=journal.journal_id,
                indexed_usn=high_usn,
                tree_size_version=TREE_SIZE_VERSION,
            )
            count = bulk_insert_entries(
                conn,
                entries.values(),
                progress=progress,
                token=token,
            )
            set_metadata(conn, entry_count=count)
            stamp_finished_metadata(conn, high_usn)
            token.check()
            conn.commit()
            conn.execute("PRAGMA optimize")
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        token.check()
        if commit:
            _replace_index(temp_path, final_path)
            report(progress, ProgressUpdate("done", count, count, f"Indexed {count:,} entries"))
            return final_path
        report(progress, ProgressUpdate("ready", count, count, f"Built {count:,} entries"))
        return StagedIndex(final_path=final_path, temp_path=temp_path, entry_count=count)
    except BaseException:
        _cleanup_temp_index(temp_path)
        raise


def build_index(
    root_path: str,
    *,
    progress: ProgressCallback | None = None,
    token: CancellationToken | None = None,
    staged: StagedIndex | None = None,
) -> str:
    result = _build_index(root_path, progress=progress, token=token, commit=True, staged=staged)
    if not isinstance(result, str):
        raise PnqiError("Internal error: index build returned an uncommitted staged index.")
    return result


def build_index_staged(
    root_path: str,
    *,
    progress: ProgressCallback | None = None,
    token: CancellationToken | None = None,
    staged: StagedIndex | None = None,
) -> StagedIndex:
    result = _build_index(root_path, progress=progress, token=token, commit=False, staged=staged)
    if not isinstance(result, StagedIndex):
        raise PnqiError("Internal error: staged index build returned a committed path.")
    return result


def _collect_changes(
    handle,
    *,
    journal_id: int,
    start_usn: int,
    stop_usn: int,
    progress: ProgressCallback | None,
    token: CancellationToken,
) -> dict[str, _Change]:
    changes: dict[str, _Change] = {}
    report(progress, ProgressUpdate("journal", 0, None, "Reading NTFS USN Journal changes"))
    for order, record in enumerate(
        read_usn_changes(handle, journal_id=journal_id, start_usn=start_usn, stop_usn=stop_usn),
        start=1,
    ):
        token.check()
        key = _frn(record.frn)
        change = changes.get(key)
        if change is None:
            change = _Change(key, record.reason, order, record)
            changes[key] = change
        else:
            change.flags |= record.reason
            change.last_record = record
        if record.reason & USN_REASON_RENAME_NEW_NAME or record.reason & USN_REASON_FILE_CREATE:
            change.new_record = record
        if record.reason & USN_REASON_FILE_DELETE:
            change.delete_record = record
        if order % 5000 == 0:
            report(progress, ProgressUpdate("journal", order, None, f"Read {order:,} USN records"))
    return changes


def _entry_from_record_path(
    conn: sqlite3.Connection,
    record: UsnRecord,
    *,
    volume_root: str,
) -> tuple[str, Entry | None, Entry | None]:
    parent = entry_by_frn(conn, _frn(record.parent_frn))
    if parent is None:
        return "", None, None
    path = join_windows_path(parent.path, record.name)
    if is_index_artifact(path, volume_root):
        return path, parent, None
    entry = _stat_entry(
        path,
        frn=_frn(record.frn),
        parent_frn=parent.frn,
        name=record.name,
        attributes=record.attributes,
        usn=record.usn,
    )
    return path, parent, entry


def _scan_filesystem_subtree(
    path: str,
    *,
    parent_frn: str,
    volume_root: str,
    usn: int,
    progress: ProgressCallback | None,
    token: CancellationToken,
) -> list[Entry]:
    root_id = get_file_id(path)
    root_stat = os.stat(path, follow_symlinks=False)
    root_attributes = int(getattr(root_stat, "st_file_attributes", root_id.attributes))
    root_entry = Entry(
        frn=_frn(root_id.frn),
        parent_frn=parent_frn,
        name=ntpath.basename(normalize_windows_path(path).rstrip("\\")) or normalize_windows_path(path),
        path=normalize_windows_path(path),
        is_dir=bool(root_attributes & FILE_ATTRIBUTE_DIRECTORY),
        size=0 if root_attributes & FILE_ATTRIBUTE_DIRECTORY else int(root_stat.st_size),
        tree_size=0 if root_attributes & FILE_ATTRIBUTE_DIRECTORY else int(root_stat.st_size),
        mtime_ns=int(root_stat.st_mtime_ns),
        attributes=root_attributes,
        usn=usn,
    )
    entries: dict[str, Entry] = {root_entry.frn: root_entry}
    stack: list[Entry] = [root_entry]
    scanned = 0
    while stack:
        token.check()
        parent = stack.pop()
        if not parent.is_dir:
            continue
        try:
            children = list(os.scandir(parent.path))
        except OSError:
            continue
        for child in children:
            token.check()
            child_path = normalize_windows_path(child.path)
            if is_index_artifact(child_path, volume_root):
                continue
            try:
                child_id = get_file_id(child_path)
                child_stat = child.stat(follow_symlinks=False)
            except OSError:
                continue
            attributes = int(getattr(child_stat, "st_file_attributes", child_id.attributes))
            child_is_dir = child.is_dir(follow_symlinks=False) or bool(attributes & FILE_ATTRIBUTE_DIRECTORY)
            size = 0 if child_is_dir else int(child_stat.st_size)
            entry = Entry(
                frn=_frn(child_id.frn),
                parent_frn=parent.frn,
                name=child.name,
                path=child_path,
                is_dir=child_is_dir,
                size=size,
                tree_size=size,
                mtime_ns=int(child_stat.st_mtime_ns),
                attributes=attributes,
                usn=usn,
            )
            entries[entry.frn] = entry
            if entry.is_dir:
                stack.append(entry)
            scanned += 1
            if scanned % 1000 == 0:
                report(
                    progress,
                    ProgressUpdate("partial-scan", scanned, None, f"Scanned {scanned:,} moved/new entries"),
                )

    entries = _deduplicate_entries_by_path(entries, root_entry.frn)
    _accumulate_entry_tree_sizes(entries, root_entry.frn)
    return list(entries.values())


def _add_subtree(
    conn: sqlite3.Connection,
    root: Entry,
    *,
    volume_root: str,
    progress: ProgressCallback | None,
    token: CancellationToken,
) -> None:
    entries = _scan_filesystem_subtree(
        root.path,
        parent_frn=root.parent_frn,
        volume_root=volume_root,
        usn=root.usn,
        progress=progress,
        token=token,
    )
    actual_root = next((entry for entry in entries if entry.frn == root.frn), root)
    for entry in entries:
        upsert_entry(conn, entry)
    update_ancestor_sizes(conn, actual_root.parent_frn, actual_root.tree_size)


def _apply_existing_change(
    conn: sqlite3.Connection,
    change: _Change,
    *,
    volume_root: str,
    root_frn: str,
    root_path: str,
    progress: ProgressCallback | None,
    token: CancellationToken,
) -> None:
    old = entry_by_frn(conn, change.frn)
    if old is None:
        return
    if old.frn == root_frn:
        if change.delete_record is not None or change.flags & (USN_REASON_RENAME_OLD_NAME | USN_REASON_RENAME_NEW_NAME):
            raise IndexInvalidError("The indexed root was deleted or moved; create a new index.")
        return
    record = change.new_record or change.last_record
    path, parent, new_entry = _entry_from_record_path(conn, record, volume_root=volume_root)
    if (
        change.delete_record is not None
        or (change.flags & USN_REASON_RENAME_OLD_NAME and change.new_record is None)
        or parent is None
        or new_entry is None
        or not _is_under(path, root_path)
    ):
        delete_subtree(conn, old.frn)
        return

    if old.is_dir != new_entry.is_dir:
        delete_subtree(conn, old.frn)
        if new_entry.is_dir:
            _add_subtree(conn, new_entry, volume_root=volume_root, progress=progress, token=token)
        else:
            upsert_entry(conn, new_entry)
            update_ancestor_sizes(conn, new_entry.parent_frn, new_entry.tree_size)
        return

    if old.is_dir:
        moved = old.parent_frn != new_entry.parent_frn or normalize_for_match(old.path) != normalize_for_match(new_entry.path)
        updated = Entry(
            frn=old.frn,
            parent_frn=new_entry.parent_frn,
            name=new_entry.name,
            path=new_entry.path,
            is_dir=True,
            size=0,
            tree_size=old.tree_size,
            mtime_ns=new_entry.mtime_ns,
            attributes=new_entry.attributes,
            usn=new_entry.usn,
        )
        if moved:
            update_ancestor_sizes(conn, old.parent_frn, -old.tree_size)
        upsert_entry(conn, updated)
        if moved:
            refresh_descendant_paths(conn, old.frn)
            update_ancestor_sizes(conn, updated.parent_frn, updated.tree_size)
        return

    delta = new_entry.size - old.size
    parent_changed = old.parent_frn != new_entry.parent_frn
    if parent_changed:
        update_ancestor_sizes(conn, old.parent_frn, -old.tree_size)
    upsert_entry(conn, new_entry)
    if parent_changed:
        update_ancestor_sizes(conn, new_entry.parent_frn, new_entry.tree_size)
    else:
        update_ancestor_sizes(conn, new_entry.parent_frn, delta)


def _apply_new_change(
    conn: sqlite3.Connection,
    change: _Change,
    *,
    volume_root: str,
    root_path: str,
    progress: ProgressCallback | None,
    token: CancellationToken,
) -> bool:
    if change.delete_record is not None:
        return True
    record = change.new_record or change.last_record
    path, parent, entry = _entry_from_record_path(conn, record, volume_root=volume_root)
    if parent is None:
        return False
    if entry is None:
        return True
    if not _is_under(path, root_path):
        return True
    if entry.is_dir:
        _add_subtree(conn, entry, volume_root=volume_root, progress=progress, token=token)
    else:
        upsert_entry(conn, entry)
        update_ancestor_sizes(conn, entry.parent_frn, entry.tree_size)
    return True


def update_index(
    path: str,
    *,
    progress: ProgressCallback | None = None,
    token: CancellationToken | None = None,
) -> str:
    validate_supported_platform()
    require_admin()
    token = token or CancellationToken()
    absolute = normalize_windows_path(str(Path(path).resolve(strict=False)))
    volume = get_volume_info(absolute)
    index_path = index_path_for_volume(volume.root)
    require_index(index_path)

    conn = connect(index_path)
    try:
        meta = validate_index(conn)
        if normalize_for_match(meta["volume_root"]) != normalize_for_match(volume.root):
            raise IndexInvalidError(f"Index {index_path} belongs to {meta['volume_root']}, not {volume.root}.")
        if int(meta["volume_serial"]) != int(volume.serial):
            raise IndexInvalidError("The volume serial number changed; create a new index.")
        if meta["filesystem"].upper() != "NTFS":
            raise IndexInvalidError("Index was not created for an NTFS volume.")
        with open_volume(volume) as handle:
            journal = query_usn_journal(handle)
            indexed_usn = int(meta["indexed_usn"])
            if str(journal.journal_id) != meta["journal_id"]:
                raise IndexInvalidError("The NTFS USN Journal was recreated; create a new index.")
            if indexed_usn < journal.lowest_valid_usn:
                raise IndexInvalidError("The NTFS USN Journal no longer contains all changes; create a new index.")
            if indexed_usn >= journal.next_usn:
                if meta.get("tree_size_version") != TREE_SIZE_VERSION:
                    conn.execute("BEGIN IMMEDIATE")
                    _repair_index_tree_sizes_if_needed(conn, meta, progress=progress, token=token)
                    stamp_finished_metadata(conn, journal.next_usn)
                    conn.commit()
                report(progress, ProgressUpdate("done", 0, 0, "Index is already fresh"))
                return index_path
            changes = _collect_changes(
                handle,
                journal_id=journal.journal_id,
                start_usn=indexed_usn,
                stop_usn=journal.next_usn,
                progress=progress,
                token=token,
            )

        if not changes:
            conn.execute("BEGIN IMMEDIATE")
            _repair_index_tree_sizes_if_needed(conn, meta, progress=progress, token=token)
            stamp_finished_metadata(conn, journal.next_usn)
            conn.commit()
            report(progress, ProgressUpdate("done", 0, 0, "No changes found"))
            return index_path

        report(progress, ProgressUpdate("apply", 0, len(changes), "Applying incremental changes"))
        conn.execute("BEGIN IMMEDIATE")
        root_path = meta["root_path"]
        root_frn = meta["root_frn"]
        try:
            _repair_index_tree_sizes_if_needed(conn, meta, progress=progress, token=token)
            existing_changes: list[_Change] = []
            new_changes: list[_Change] = []
            for change in sorted(changes.values(), key=lambda item: item.first_order):
                token.check()
                if entry_by_frn(conn, change.frn) is None:
                    new_changes.append(change)
                else:
                    existing_changes.append(change)

            applied = 0
            for change in existing_changes:
                token.check()
                _apply_existing_change(
                    conn,
                    change,
                    volume_root=volume.root,
                    root_frn=root_frn,
                    root_path=root_path,
                    progress=progress,
                    token=token,
                )
                applied += 1
                if applied % 500 == 0:
                    report(progress, ProgressUpdate("apply", applied, len(changes), f"Applied {applied:,} changes"))

            pending = new_changes
            while pending:
                next_pending: list[_Change] = []
                made_progress = False
                for change in pending:
                    token.check()
                    if entry_by_frn(conn, change.frn) is not None:
                        _apply_existing_change(
                            conn,
                            change,
                            volume_root=volume.root,
                            root_frn=root_frn,
                            root_path=root_path,
                            progress=progress,
                            token=token,
                        )
                        made_progress = True
                        applied += 1
                    elif _apply_new_change(
                        conn,
                        change,
                        volume_root=volume.root,
                        root_path=root_path,
                        progress=progress,
                        token=token,
                    ):
                        made_progress = True
                        applied += 1
                    else:
                        next_pending.append(change)
                    if applied % 500 == 0:
                        report(
                            progress,
                            ProgressUpdate("apply", applied, len(changes), f"Applied {applied:,} changes"),
                        )
                if not made_progress:
                    break
                pending = next_pending

            stamp_finished_metadata(conn, journal.next_usn)
            set_metadata(conn, entry_count=count_entries(conn))
            token.check()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        report(progress, ProgressUpdate("done", len(changes), len(changes), f"Applied {len(changes):,} changes"))
        return index_path
    finally:
        conn.close()


def ensure_fresh_index_for_path(
    path: str,
    *,
    progress: ProgressCallback | None = None,
    token: CancellationToken | None = None,
) -> str:
    absolute = normalize_windows_path(str(Path(path).resolve(strict=False)))
    volume = get_volume_info(absolute)
    index_path = index_path_for_volume(volume.root)
    require_index(index_path)
    return update_index(absolute, progress=progress, token=token)


def search(
    pattern: str,
    *,
    limit: int = 0,
    progress: ProgressCallback | None = None,
    token: CancellationToken | None = None,
) -> list[Entry]:
    return list(iter_search(pattern, limit=limit, progress=progress, token=token))


def iter_search(
    pattern: str,
    *,
    limit: int = 0,
    progress: ProgressCallback | None = None,
    token: CancellationToken | None = None,
) -> Iterator[Entry]:
    token = token or CancellationToken()
    volume = _volume_for_pattern(pattern)
    index_path = update_index(volume.root, progress=progress, token=token)
    like = sqlite_like_from_star_pattern(pattern)
    conn = connect(index_path, readonly=True)
    found = 0
    total = 0
    try:
        with cancellable_query(conn, token):
            total = count_entries(conn, "path_norm LIKE ? ESCAPE '\\'", (like,))
            effective_total = min(total, limit) if limit > 0 else total
            report(progress, ProgressUpdate("search", 0, effective_total, "Searching index"))
            sql = """
                SELECT * FROM entries
                WHERE path_norm LIKE ? ESCAPE '\\'
                ORDER BY tree_size DESC, path_norm
            """
            params: tuple[object, ...]
            if limit > 0:
                sql += " LIMIT ?"
                params = (like, limit)
            else:
                params = (like,)
            cursor = conn.execute(sql, params)
            for idx, row in enumerate(cursor, start=1):
                token.check()
                found = idx
                yield row_to_entry(row)
                if idx % 500 == 0:
                    report(progress, ProgressUpdate("search", idx, effective_total, f"Found {idx:,} matches"))
        report(progress, ProgressUpdate("done", found, effective_total, f"Found {found:,} matches"))
    finally:
        conn.close()


def list_sizes(
    path: str,
    *,
    recursive: bool = True,
    progress: ProgressCallback | None = None,
    token: CancellationToken | None = None,
) -> list[Entry]:
    token = token or CancellationToken()
    index_path = ensure_fresh_index_for_path(path, progress=progress, token=token)
    target_path = normalize_windows_path(str(Path(path).resolve(strict=False)))
    conn = connect(index_path, readonly=True)
    try:
        root = entry_by_path(conn, target_path)
        if root is None:
            raise IndexNotFoundError(f"{target_path} is not inside the indexed root.")
        if not root.is_dir:
            return [root]
        with cancellable_query(conn, token):
            if recursive:
                rows = conn.execute(
                    """
                    WITH RECURSIVE subtree(frn) AS (
                        SELECT frn FROM entries WHERE parent_frn = ? AND frn != ?
                        UNION ALL
                        SELECT e.frn FROM entries e JOIN subtree s ON e.parent_frn = s.frn
                        WHERE e.frn != e.parent_frn
                    )
                    SELECT e.* FROM entries e
                    JOIN subtree s ON e.frn = s.frn
                    ORDER BY e.tree_size DESC, e.path_norm
                    """,
                    (root.frn, root.frn),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM entries
                    WHERE parent_frn = ? AND frn != ?
                    ORDER BY tree_size DESC, path_norm
                    """,
                    (root.frn, root.frn),
                ).fetchall()
            result = [row_to_entry(row) for row in rows]
            report(progress, ProgressUpdate("done", len(result), len(result), f"Loaded {len(result):,} entries"))
            return result
    finally:
        conn.close()


def browse_children(
    path: str,
    *,
    progress: ProgressCallback | None = None,
    token: CancellationToken | None = None,
) -> tuple[Entry, list[Entry]]:
    token = token or CancellationToken()
    index_path = ensure_fresh_index_for_path(path, progress=progress, token=token)
    target_path = normalize_windows_path(str(Path(path).resolve(strict=False)))
    conn = connect(index_path, readonly=True)
    try:
        root = entry_by_path(conn, target_path)
        if root is None:
            raise IndexNotFoundError(f"{target_path} is not inside the indexed root.")
        rows = conn.execute(
            """
            SELECT * FROM entries
            WHERE parent_frn = ? AND frn != ?
            ORDER BY is_dir DESC, tree_size DESC, name COLLATE NOCASE
            """,
            (root.frn, root.frn),
        ).fetchall()
        return root, [row_to_entry(row) for row in rows]
    finally:
        conn.close()


def refresh_known_indexes(
    *,
    progress: ProgressCallback | None = None,
    token: CancellationToken | None = None,
) -> list[str]:
    token = token or CancellationToken()
    refreshed: list[str] = []
    for drive in string.ascii_uppercase:
        token.check()
        root = f"{drive}:\\"
        index_path = index_path_for_volume(root)
        if not os.path.exists(index_path):
            continue
        try:
            get_volume_info(root)
            refreshed.append(update_index(root, progress=progress, token=token))
        except (IndexInvalidError, IndexNotFoundError, PnqiError):
            continue
    return refreshed
