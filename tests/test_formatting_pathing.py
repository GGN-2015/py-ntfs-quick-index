from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pnqi.db import (
    Entry,
    SQLITE_INT64_MAX,
    bulk_insert_entries,
    connect,
    create_schema,
    descendant_frns,
    entry_by_frn,
    recompute_tree_sizes,
    update_ancestor_sizes,
    upsert_entry,
)
from pnqi.cli import _pattern_in_drive, _validate_limit
from pnqi.errors import IndexInvalidError, PnqiError
from pnqi.formatting import human_mtime, human_percent, human_size
from pnqi.indexer import (
    _accumulate_entry_tree_sizes,
    _build_index_from_filesystem,
    _deduplicate_entries_by_path,
    _recover_index_from_filesystem,
    _replace_index,
)
from pnqi.lock import acquire_index_lock, lock_paths
from pnqi.pathing import normalize_windows_path, sqlite_like_from_star_pattern
from pnqi.progress import CancellationToken
from pnqi.winapi import ERROR_JOURNAL_ENTRY_DELETED, FILE_ATTRIBUTE_DIRECTORY, read_usn_changes


class FormattingTests(unittest.TestCase):
    def test_human_size_uses_readable_decimal_units(self) -> None:
        self.assertEqual(human_size(999), "999 B")
        self.assertEqual(human_size(1000), "1 KB")
        self.assertEqual(human_size(123456789), "123.457 MB")

    def test_human_percent_uses_compact_decimal_text(self) -> None:
        self.assertEqual(human_percent(0, 0), "0%")
        self.assertEqual(human_percent(1, 4), "25%")
        self.assertEqual(human_percent(1, 3), "33.333%")

    def test_human_mtime_handles_out_of_range_values(self) -> None:
        self.assertEqual(human_mtime(10**30), "Out of range")


class PathingTests(unittest.TestCase):
    def test_normalize_drive_root_keeps_backslash(self) -> None:
        self.assertEqual(normalize_windows_path("c:/"), "C:\\")

    def test_star_pattern_escapes_backslashes_for_sqlite_like(self) -> None:
        self.assertEqual(
            sqlite_like_from_star_pattern("C:/Users/*/Desktop/*"),
            "c:\\\\users\\\\%\\\\desktop\\\\%",
        )

    def test_posix_path_helpers_preserve_forward_slashes(self) -> None:
        with (
            patch("pnqi.pathing.is_windows", return_value=False),
            patch("pnqi.db.is_windows", return_value=False),
        ):
            self.assertEqual(normalize_windows_path("/mnt/ntfs//Users"), "/mnt/ntfs/Users")
            self.assertEqual(sqlite_like_from_star_pattern("/mnt/ntfs/*/Desktop/*"), "/mnt/ntfs/%/desktop/%")

    def test_cli_drive_pattern_resolves_relative_patterns(self) -> None:
        self.assertEqual(_pattern_in_drive("C:\\", "Users\\*"), "C:\\Users\\*")

    def test_cli_drive_pattern_resolves_posix_relative_patterns(self) -> None:
        with (
            patch("pnqi.cli.is_windows", return_value=False),
            patch("pnqi.pathing.is_windows", return_value=False),
        ):
            self.assertEqual(_pattern_in_drive("/mnt/ntfs", "Users/*"), "/mnt/ntfs/Users/*")

    def test_cli_drive_pattern_rejects_other_drives(self) -> None:
        with self.assertRaises(PnqiError):
            _pattern_in_drive("C:\\", "D:\\*")

    def test_cli_limit_rejects_negative_values(self) -> None:
        with self.assertRaises(PnqiError):
            _validate_limit(-1)
        _validate_limit(0)


class DatabaseTests(unittest.TestCase):
    def test_descendant_query_walks_nested_children(self) -> None:
        with TemporaryDirectory() as temp_dir:
            conn = connect(str(Path(temp_dir) / "index.sqlite"))
            try:
                create_schema(conn)
                upsert_entry(
                    conn,
                    Entry("1", "1", "root", "C:\\root", True, 0, 30, 0, 0, 1),
                )
                upsert_entry(
                    conn,
                    Entry("2", "1", "child", "C:\\root\\child", True, 0, 20, 0, 0, 1),
                )
                upsert_entry(
                    conn,
                    Entry("3", "2", "file.txt", "C:\\root\\child\\file.txt", False, 20, 20, 0, 0, 1),
                )
                self.assertEqual(descendant_frns(conn, "1"), ["2", "3"])
            finally:
                conn.close()

    def test_recompute_tree_sizes_sums_deep_descendant_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            conn = connect(str(Path(temp_dir) / "index.sqlite"))
            try:
                create_schema(conn)
                upsert_entry(conn, Entry("1", "1", "root", "C:\\root", True, 0, 0, 0, 0, 1))
                upsert_entry(
                    conn,
                    Entry("2", "1", "child", "C:\\root\\child", True, 0, 0, 0, 0, 1),
                )
                upsert_entry(
                    conn,
                    Entry("3", "2", "grand", "C:\\root\\child\\grand", True, 0, 0, 0, 0, 1),
                )
                upsert_entry(
                    conn,
                    Entry(
                        "4",
                        "3",
                        "empty.bin",
                        "C:\\root\\child\\grand\\empty.bin",
                        False,
                        0,
                        0,
                        0,
                        0,
                        1,
                    ),
                )
                upsert_entry(
                    conn,
                    Entry(
                        "5",
                        "3",
                        "data.bin",
                        "C:\\root\\child\\grand\\data.bin",
                        False,
                        42,
                        42,
                        0,
                        0,
                        1,
                    ),
                )
                recompute_tree_sizes(conn)
                self.assertEqual(entry_by_frn(conn, "1").tree_size, 42)  # type: ignore[union-attr]
                self.assertEqual(entry_by_frn(conn, "2").tree_size, 42)  # type: ignore[union-attr]
                self.assertEqual(entry_by_frn(conn, "3").tree_size, 42)  # type: ignore[union-attr]
            finally:
                conn.close()

    def test_initial_tree_size_accumulator_uses_updated_child_sizes(self) -> None:
        entries = {
            "1": Entry("1", "1", "root", "C:\\root", True, 0, 0, 0, 0, 1),
            "2": Entry("2", "1", "child", "C:\\root\\child", True, 0, 0, 0, 0, 1),
            "3": Entry("3", "2", "grand", "C:\\root\\child\\grand", True, 0, 0, 0, 0, 1),
            "4": Entry(
                "4",
                "3",
                "data.bin",
                "C:\\root\\child\\grand\\data.bin",
                False,
                42,
                42,
                0,
                0,
                1,
            ),
        }
        _accumulate_entry_tree_sizes(entries, "1")
        self.assertEqual(entries["1"].tree_size, 42)
        self.assertEqual(entries["2"].tree_size, 42)
        self.assertEqual(entries["3"].tree_size, 42)

    def test_initial_path_deduplication_keeps_current_file_id_match(self) -> None:
        entries = {
            "1": Entry("1", "1", "root", "C:\\root", True, 0, 0, 0, 0, 1),
            "2": Entry("2", "1", "same.txt", "C:\\root\\same.txt", False, 5, 5, 0, 0, 1),
            "3": Entry("3", "1", "SAME.txt", "C:\\root\\SAME.txt", False, 7, 7, 0, 0, 2),
        }

        with patch("pnqi.indexer.get_file_id", return_value=SimpleNamespace(frn=3)):
            deduped = _deduplicate_entries_by_path(entries, "1")

        self.assertIn("1", deduped)
        self.assertNotIn("2", deduped)
        self.assertIn("3", deduped)

    def test_initial_path_deduplication_drops_orphans(self) -> None:
        entries = {
            "1": Entry("1", "1", "root", "C:\\root", True, 0, 0, 0, 0, 1),
            "2": Entry("2", "1", "Folder", "C:\\root\\Folder", True, 0, 0, 0, 0, 1),
            "3": Entry("3", "1", "folder", "C:\\root\\folder", True, 0, 0, 0, 0, 2),
            "4": Entry("4", "2", "child.txt", "C:\\root\\Folder\\child.txt", False, 4, 4, 0, 0, 1),
        }

        with patch("pnqi.indexer.get_file_id", return_value=SimpleNamespace(frn=3)):
            deduped = _deduplicate_entries_by_path(entries, "1")

        self.assertIn("3", deduped)
        self.assertNotIn("2", deduped)
        self.assertNotIn("4", deduped)

    def test_upsert_replaces_stale_entry_with_same_normalized_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            conn = connect(str(Path(temp_dir) / "index.sqlite"))
            try:
                create_schema(conn)
                upsert_entry(conn, Entry("1", "1", "root", "C:\\root", True, 0, 0, 0, 0, 1))
                upsert_entry(
                    conn,
                    Entry("2", "1", "old.bin", "C:\\root\\old.bin", False, 5, 5, 0, 0, 1),
                )
                upsert_entry(
                    conn,
                    Entry("3", "1", "OLD.bin", "C:\\root\\OLD.bin", False, 7, 7, 0, 0, 2),
                )

                self.assertIsNone(entry_by_frn(conn, "2"))
                replacement = entry_by_frn(conn, "3")
                self.assertIsNotNone(replacement)
                self.assertEqual(replacement.size, 7)  # type: ignore[union-attr]
            finally:
                conn.close()

    def test_bulk_insert_ignores_duplicate_normalized_paths(self) -> None:
        with TemporaryDirectory() as temp_dir:
            conn = connect(str(Path(temp_dir) / "index.sqlite"))
            try:
                create_schema(conn)
                count = bulk_insert_entries(
                    conn,
                    [
                        Entry("1", "1", "root", "C:\\root", True, 0, 0, 0, 0, 1),
                        Entry("2", "1", "same.txt", "C:\\root\\same.txt", False, 5, 5, 0, 0, 1),
                        Entry("3", "1", "SAME.txt", "C:\\root\\SAME.txt", False, 7, 7, 0, 0, 2),
                    ],
                )

                self.assertEqual(count, 2)
                self.assertIsNotNone(entry_by_frn(conn, "2"))
                self.assertIsNone(entry_by_frn(conn, "3"))
            finally:
                conn.close()

    def test_oversized_entry_integers_are_clamped_for_sqlite(self) -> None:
        with TemporaryDirectory() as temp_dir:
            conn = connect(str(Path(temp_dir) / "index.sqlite"))
            try:
                create_schema(conn)
                upsert_entry(
                    conn,
                    Entry(
                        "1",
                        "1",
                        "huge.bin",
                        "C:\\root\\huge.bin",
                        False,
                        2**80,
                        2**80,
                        2**80,
                        2**80,
                        2**80,
                    ),
                )

                entry = entry_by_frn(conn, "1")
                self.assertIsNotNone(entry)
                self.assertEqual(entry.size, SQLITE_INT64_MAX)  # type: ignore[union-attr]
                self.assertEqual(entry.tree_size, SQLITE_INT64_MAX)  # type: ignore[union-attr]
                self.assertEqual(entry.mtime_ns, SQLITE_INT64_MAX)  # type: ignore[union-attr]
                self.assertEqual(entry.attributes, SQLITE_INT64_MAX)  # type: ignore[union-attr]
                self.assertEqual(entry.usn, SQLITE_INT64_MAX)  # type: ignore[union-attr]
            finally:
                conn.close()

    def test_ancestor_size_updates_clamp_overflow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            conn = connect(str(Path(temp_dir) / "index.sqlite"))
            try:
                create_schema(conn)
                upsert_entry(
                    conn,
                    Entry("1", "1", "root", "C:\\root", True, 0, SQLITE_INT64_MAX - 1, 0, 0, 1),
                )
                upsert_entry(
                    conn,
                    Entry("2", "1", "huge.bin", "C:\\root\\huge.bin", False, 10, 10, 0, 0, 1),
                )

                update_ancestor_sizes(conn, "1", 10)

                root = entry_by_frn(conn, "1")
                self.assertIsNotNone(root)
                self.assertEqual(root.tree_size, SQLITE_INT64_MAX)  # type: ignore[union-attr]
            finally:
                conn.close()

    def test_replace_index_moves_temp_file_to_final_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            temp_path = base / "index.sqlite.tmp"
            final_path = base / "index.sqlite"
            temp_path.write_text("new index", encoding="utf-8")
            final_path.write_text("old index", encoding="utf-8")

            _replace_index(str(temp_path), str(final_path))

            self.assertFalse(temp_path.exists())
            self.assertEqual(final_path.read_text(encoding="utf-8"), "new index")

    def test_replace_index_reports_missing_temp_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            with self.assertRaises(PnqiError):
                _replace_index(str(base / "missing.sqlite.tmp"), str(base / "index.sqlite"))

    def test_deleted_usn_journal_entry_requires_reindex(self) -> None:
        handle = SimpleNamespace(value=1)
        with patch(
            "pnqi.winapi._device_io_control",
            return_value=(False, 0, ERROR_JOURNAL_ENTRY_DELETED),
        ):
            with self.assertRaisesRegex(IndexInvalidError, "create a new index"):
                list(read_usn_changes(handle, journal_id=1, start_usn=1, stop_usn=2))

    def test_recover_index_from_filesystem_replaces_stale_entries(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = normalize_windows_path(temp_dir)
            file_path = normalize_windows_path(str(Path(temp_dir) / "current.txt"))
            Path(file_path).write_text("hello", encoding="utf-8")
            drive = root[:2] + "\\"
            volume = SimpleNamespace(root=drive, serial=123, filesystem="NTFS")
            conn = connect(str(Path(temp_dir) / "index.sqlite"))
            try:
                create_schema(conn)
                upsert_entry(conn, Entry("99", "99", "stale.txt", root + "\\stale.txt", False, 1, 1, 0, 0, 1))
                conn.commit()
                meta = {
                    "root_path": root,
                    "root_frn": "99",
                }

                def fake_get_file_id(path: str) -> SimpleNamespace:
                    normalized = normalize_windows_path(path)
                    if normalized == root:
                        return SimpleNamespace(frn=1, volume_serial=123, attributes=FILE_ATTRIBUTE_DIRECTORY)
                    if normalized == file_path:
                        return SimpleNamespace(frn=2, volume_serial=123, attributes=0)
                    return SimpleNamespace(frn=10, volume_serial=123, attributes=FILE_ATTRIBUTE_DIRECTORY)

                journal = SimpleNamespace(next_usn=50, journal_id=77)
                context = MagicMock()
                context.__enter__.return_value = SimpleNamespace(value=1)
                context.__exit__.return_value = None
                with (
                    patch("pnqi.indexer.get_file_id", side_effect=fake_get_file_id),
                    patch("pnqi.indexer.open_volume", return_value=context),
                    patch("pnqi.indexer.query_usn_journal", return_value=journal),
                ):
                    high_usn = _recover_index_from_filesystem(
                        conn,
                        meta,
                        volume=volume,
                        progress=None,
                        token=SimpleNamespace(check=lambda: None),
                    )

                self.assertEqual(high_usn, 50)
                self.assertIsNone(entry_by_frn(conn, "99"))
                current = entry_by_frn(conn, "2")
                self.assertIsNotNone(current)
                self.assertEqual(current.path, file_path)  # type: ignore[union-attr]
                metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM metadata")}
                self.assertEqual(metadata["indexed_usn"], "50")
                self.assertEqual(metadata["journal_id"], "77")
                self.assertEqual(metadata["root_frn"], "1")
            finally:
                conn.close()

    def test_portable_filesystem_build_writes_sqlite_index(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = normalize_windows_path(str(Path(temp_dir) / "root"))
            Path(root).mkdir()
            file_path = normalize_windows_path(str(Path(root) / "file.txt"))
            Path(file_path).write_text("hello", encoding="utf-8")
            final_path = normalize_windows_path(str(Path(temp_dir) / "pnqi.index.sqlite"))
            volume = SimpleNamespace(root=normalize_windows_path(temp_dir), serial=123, filesystem="NTFS")

            def fake_get_file_id(path: str) -> SimpleNamespace:
                normalized = normalize_windows_path(path)
                if normalized == root:
                    return SimpleNamespace(frn="root", volume_serial=123, attributes=FILE_ATTRIBUTE_DIRECTORY)
                if normalized == file_path:
                    return SimpleNamespace(frn="file", volume_serial=123, attributes=0)
                return SimpleNamespace(frn="parent", volume_serial=123, attributes=FILE_ATTRIBUTE_DIRECTORY)

            with (
                patch(
                    "pnqi.indexer._resolve_index_path_for_existing_path",
                    return_value=(root, final_path, volume),
                ),
                patch("pnqi.indexer.get_file_id", side_effect=fake_get_file_id),
                patch("pnqi.indexer._uses_windows_ntfs_backend", return_value=False),
            ):
                result = _build_index_from_filesystem(root, commit=True, token=CancellationToken())

            self.assertEqual(result, final_path)
            self.assertTrue(Path(final_path).exists())
            conn = connect(final_path, readonly=True)
            try:
                entry = entry_by_frn(conn, "file")
                self.assertIsNotNone(entry)
                self.assertEqual(entry.path, file_path)  # type: ignore[union-attr]
            finally:
                conn.close()

    def test_index_work_lock_writes_status_and_releases(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"PNQI_STATE_DIR": temp_dir}):
                lock_path, status_path = lock_paths()

                with acquire_index_lock(
                    "test-operation",
                    "C:\\pnqi.index.sqlite",
                    token=CancellationToken(),
                ):
                    self.assertTrue(lock_path.exists())
                    self.assertTrue(status_path.exists())
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                    self.assertEqual(status["operation"], "test-operation")
                    self.assertEqual(status["target"], "C:\\pnqi.index.sqlite")

                self.assertFalse(lock_path.exists())
                self.assertFalse(status_path.exists())

    def test_index_work_lock_recovers_stale_status(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"PNQI_STATE_DIR": temp_dir}):
                lock_path, status_path = lock_paths()
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                stale = {"pid": -1, "operation": "old", "target": "stale"}
                lock_path.write_text(json.dumps(stale), encoding="utf-8")
                status_path.write_text(json.dumps(stale), encoding="utf-8")

                with acquire_index_lock("new", "target", token=CancellationToken()):
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                    self.assertEqual(status["operation"], "new")


if __name__ == "__main__":
    unittest.main()
