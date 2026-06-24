from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pnqi.formatting import human_size
from pnqi.pathing import normalize_windows_path, sqlite_like_from_star_pattern
from pnqi.db import Entry, connect, create_schema, descendant_frns, upsert_entry


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


if __name__ == "__main__":
    unittest.main()
