"""Checks for catalog persistence and the filesystem walk.

Every test runs against a temporary fixture; none of them scans the developer's
whole machine.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from texman import index


def build_fixture(root: Path) -> dict[str, Path]:
    """Create nested TeX files, hidden folders, spaces, and a symlink loop."""
    made: dict[str, Path] = {}

    (root / "papers" / "thesis").mkdir(parents=True)
    (root / "papers" / "notes with spaces").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "texmf" / "tex" / "latex").mkdir(parents=True)
    # A separate directory, so the mixed-case name does not collide with
    # custom.sty on a case-insensitive filesystem.
    (root / "texmf" / "tex" / "generic").mkdir(parents=True)
    (root / "not-tex").mkdir()

    made["main"] = root / "papers" / "thesis" / "main.tex"
    made["chapter"] = root / "papers" / "thesis" / "chapter.tex"
    # Duplicate filename in a different directory.
    made["other_main"] = root / "papers" / "notes with spaces" / "main.tex"
    made["spaces"] = root / "papers" / "notes with spaces" / "my notes.tex"
    made["hidden"] = root / ".hidden" / "secret.tex"
    made["upper"] = root / "papers" / "UPPER.TEX"
    made["mixed_sty"] = root / "texmf" / "tex" / "generic" / "Custom.Sty"
    made["sty"] = root / "texmf" / "tex" / "latex" / "custom.sty"
    for path in made.values():
        path.write_text("\\documentclass{article}\n")

    # Non-matching files must be ignored.
    (root / "not-tex" / "readme.md").write_text("no\n")
    (root / "not-tex" / "texture.png").write_bytes(b"\x89PNG")

    # A symlink loop and a broken link must not derail the walk.
    os.symlink(root, root / "papers" / "loop")
    os.symlink(root / "papers", root / "papers" / "thesis" / "up")
    os.symlink(root / "missing.tex", root / "broken.tex")
    return made


class WalkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.files = build_fixture(self.root)
        self.addCleanup(self.tmp.cleanup)

    def scan_paths(self, **kwargs) -> tuple[set[str], index.ScanStats]:
        stats = index.ScanStats()
        found = {r.path for r in index.walk_tree(self.root, stats, **kwargs)}
        return found, stats

    def test_finds_every_matching_file_once(self) -> None:
        found, stats = self.scan_paths()
        expected = {str(p.resolve()) for p in self.files.values()}
        self.assertEqual(found, expected)
        self.assertEqual(stats.files_found, len(expected))

    def test_matches_extensions_case_insensitively(self) -> None:
        found, _ = self.scan_paths()
        self.assertIn(str(self.files["upper"].resolve()), found)
        self.assertIn(str(self.files["mixed_sty"].resolve()), found)

    def test_skips_non_matching_and_broken_links(self) -> None:
        found, _ = self.scan_paths()
        self.assertFalse([p for p in found if p.endswith((".md", ".png"))])
        self.assertNotIn(str(self.root / "broken.tex"), found)

    def test_symlink_loop_terminates_without_duplicates(self) -> None:
        found, stats = self.scan_paths()
        self.assertEqual(len(found), stats.files_found)
        self.assertTrue(all("loop" not in p for p in found))

    def test_records_extension_and_parent_directory(self) -> None:
        stats = index.ScanStats()
        records = list(index.walk_tree(self.root, stats))
        by_name = {os.path.basename(r.path): r for r in records}
        self.assertEqual(by_name["main.tex"].extension, ".tex")
        self.assertEqual(by_name["UPPER.TEX"].extension, ".tex")
        self.assertEqual(by_name["Custom.Sty"].extension, ".sty")
        self.assertEqual(
            by_name["my notes.tex"].parent_dir,
            str(self.root / "papers" / "notes with spaces"),
        )

    def test_cancellation_stops_and_reports_incomplete(self) -> None:
        stats = index.ScanStats()
        walker = index.walk_tree(self.root, stats, should_cancel=lambda: True)
        self.assertEqual(list(walker), [])
        self.assertTrue(stats.cancelled)
        self.assertFalse(stats.complete)

    def test_permission_failure_is_counted_and_walk_continues(self) -> None:
        locked = self.root / "locked"
        locked.mkdir()
        (locked / "inside.tex").write_text("x")
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, stat.S_IRWXU)
        found, stats = self.scan_paths()
        if os.geteuid() == 0:  # pragma: no cover - root ignores permissions
            self.skipTest("running as root defeats the permission check")
        self.assertGreaterEqual(stats.skipped, 1)
        self.assertTrue(stats.skipped_samples)
        self.assertIn(str(self.files["main"].resolve()), found)

    def test_progress_reports_current_directory(self) -> None:
        seen: list[str] = []
        stats = index.ScanStats()
        list(
            index.walk_tree(
                self.root,
                stats,
                on_progress=lambda s: seen.append(s.current_dir),
                progress_interval=0.0,
            )
        )
        self.assertTrue(seen)
        self.assertEqual(stats.current_dir, "")

    def test_excluded_roots_reported_only_when_under_root(self) -> None:
        _, stats = self.scan_paths()
        self.assertEqual(stats.excluded_roots, [])
        self.assertIn("/dev", index._excluded_for("/"))


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.files = build_fixture(self.root)
        self.db = self.root / "index.sqlite3"
        self.addCleanup(self.tmp.cleanup)

    def scan(self, **kwargs) -> index.ScanStats:
        return index.run_scan(self.root, db_path=self.db, **kwargs)

    def test_scan_populates_catalog_and_directories(self) -> None:
        stats = self.scan()
        self.assertTrue(stats.complete)
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(index.count_files(conn), len(self.files))
        directories = dict(index.list_directories(conn))
        self.assertEqual(
            directories[str(self.root / "papers" / "thesis")], 2
        )

    def test_description_survives_restart_and_rescan(self) -> None:
        self.scan()
        target = str(self.files["main"].resolve())
        conn = index.connect(self.db)
        index.set_description(conn, target, "Thesis: main document")
        conn.close()

        self.scan()  # rescan with a fresh connection
        conn = index.connect(self.db)  # reopen, as a restart would
        self.addCleanup(conn.close)
        entry = index.get_file(conn, target)
        assert entry is not None
        self.assertEqual(entry.description, "Thesis: main document")

    def test_description_survives_cancelled_scan(self) -> None:
        self.scan()
        target = str(self.files["sty"].resolve())
        conn = index.connect(self.db)
        index.set_description(conn, target, "custom package")
        conn.close()

        stats = self.scan(should_cancel=lambda: True)
        self.assertFalse(stats.complete)
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        entry = index.get_file(conn, target)
        assert entry is not None
        self.assertEqual(entry.description, "custom package")

    def test_missing_file_keeps_row_and_description(self) -> None:
        self.scan()
        target = str(self.files["chapter"].resolve())
        conn = index.connect(self.db)
        index.set_description(conn, target, "chapter two")
        conn.close()

        os.unlink(target)
        self.scan()
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        entry = index.get_file(conn, target)
        assert entry is not None
        self.assertEqual(entry.description, "chapter two")
        self.assertFalse(entry.available)

    def test_descriptions_with_quotes_are_stored_verbatim(self) -> None:
        self.scan()
        target = str(self.files["main"].resolve())
        tricky = "it's a \"draft\"; DROP TABLE files; --"
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        index.set_description(conn, target, tricky)
        entry = index.get_file(conn, target)
        assert entry is not None
        self.assertEqual(entry.description, tricky)
        self.assertEqual(index.count_files(conn), len(self.files))

    def test_filter_matches_path_and_description_case_insensitively(self) -> None:
        self.scan()
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        index.set_description(conn, str(self.files["sty"].resolve()), "Shared MACROS")
        by_path = index.list_files(conn, query="NOTES WITH SPACES")
        self.assertEqual(len(by_path), 2)
        by_description = index.list_files(conn, query="macros")
        self.assertEqual([e.path for e in by_description], [str(self.files["sty"].resolve())])

    def test_directory_filter_restricts_results(self) -> None:
        self.scan()
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        thesis = str(self.root / "papers" / "thesis")
        entries = index.list_files(conn, parent_dir=thesis)
        self.assertEqual({e.name for e in entries}, {"main.tex", "chapter.tex"})

    def test_batches_are_committed_as_they_arrive(self) -> None:
        seen: list[int] = []

        def on_batch(records, stats) -> None:
            seen.append(len(records))
            # A concurrent reader must already see the committed rows.
            reader = index.connect(self.db)
            self.assertGreaterEqual(index.count_files(reader), len(records))
            reader.close()

        self.scan(on_batch=on_batch, batch_size=2)
        self.assertTrue(seen)
        self.assertEqual(sum(seen), len(self.files))

    def test_rescan_updates_last_seen_without_touching_description(self) -> None:
        self.scan()
        target = str(self.files["main"].resolve())
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        index.set_description(conn, target, "keep me")
        before = index.get_file(conn, target)
        assert before is not None
        conn.execute("UPDATE files SET last_seen = '1999-01-01T00:00:00+00:00' WHERE path = ?", (target,))
        conn.commit()
        self.scan()
        after = index.get_file(conn, target)
        assert after is not None
        self.assertNotEqual(after.last_seen, "1999-01-01T00:00:00+00:00")
        self.assertEqual(after.description, "keep me")

    def test_db_path_honours_environment(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": str(self.root / "xdg")}, clear=False
        ):
            os.environ.pop("TEXMAN_DB", None)
            self.assertEqual(
                index.default_db_path(), self.root / "xdg" / "texman" / "index.sqlite3"
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
