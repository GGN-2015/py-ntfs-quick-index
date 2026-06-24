from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pnqi.db import (
    Entry,
    connect,
    create_schema,
    descendant_frns,
    entry_by_frn,
    recompute_tree_sizes,
    upsert_entry,
)
from pnqi.formatting import human_size
from pnqi.indexer import _accumulate_entry_tree_sizes
from pnqi.pathing import normalize_windows_path, sqlite_like_from_star_pattern


class FormattingTests(unittest.TestCase):
    def test_human_size_uses_readable_decimal_units(self) -> None:
        self.assertEqual(human_size(999), "999 B")
        self.assertEqual(human_size(1000), "1 KB")
        self.assertEqual(human_size(123456789), "123.457 MB")


class PathingTests(unittest.TestCase):
    def test_normalize_drive_root_keeps_backslash(self) -> None:
        self.assertEqual(normalize_windows_path("c:/"), "C:\\")

    def test_star_pattern_escapes_backslashes_for_sqlite_like(self) -> None:
        self.assertEqual(
            sqlite_like_from_star_pattern("C:/Users/*/Desktop/*"),
            "c:\\\\users\\\\%\\\\desktop\\\\%",
        )


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


if __name__ == "__main__":
    unittest.main()
