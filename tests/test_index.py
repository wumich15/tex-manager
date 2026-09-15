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

    def test_name_that_is_only_an_extension_keeps_its_extension(self) -> None:
        # os.path.splitext reports no extension for a bare ".tex" name.
        bare = self.root / "papers" / ".tex"
        bare.write_text("x")
        stats = index.ScanStats()
        records = {r.path: r for r in index.walk_tree(self.root, stats)}
        self.assertEqual(records[str(bare)].extension, ".tex")

    def test_a_file_given_as_the_root_is_not_counted_as_a_directory(self) -> None:
        stats = index.ScanStats()
        found = list(index.walk_tree(self.files["main"], stats))
        self.assertEqual(found, [])
        self.assertEqual(stats.dirs_visited, 0)
        self.assertEqual(stats.skipped, 1)

    def test_missing_root_is_reported_as_skipped(self) -> None:
        stats = index.ScanStats()
        self.assertEqual(list(index.walk_tree(self.root / "nope", stats)), [])
        self.assertEqual(stats.dirs_visited, 0)
        self.assertEqual(stats.skipped, 1)

    def test_excluded_roots_reported_only_when_under_root(self) -> None:
        _, stats = self.scan_paths()
        self.assertEqual(stats.excluded_roots, [])
        self.assertIn("/dev", index._excluded_for("/"))


class RootTests(unittest.TestCase):
    """The default roots, and walking more than one of them."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def _as_home(self):
        return unittest.mock.patch.dict(os.environ, {"HOME": str(self.home)})

    def test_default_roots_are_documents_and_downloads(self) -> None:
        (self.home / "Documents").mkdir()
        (self.home / "Downloads").mkdir()
        with self._as_home():
            self.assertEqual(
                index.default_scan_roots(),
                [str(self.home / "Documents"), str(self.home / "Downloads")],
            )

    def test_a_missing_default_root_is_dropped_not_fatal(self) -> None:
        (self.home / "Downloads").mkdir()
        with self._as_home():
            self.assertEqual(
                index.default_scan_roots(), [str(self.home / "Downloads")]
            )

    def test_default_roots_are_empty_when_neither_exists(self) -> None:
        with self._as_home():
            self.assertEqual(index.default_scan_roots(), [])

    def test_display_path_abbreviates_the_home_directory(self) -> None:
        with self._as_home():
            self.assertEqual(
                index.display_path(str(self.home / "Documents")), "~/Documents"
            )
            self.assertEqual(index.display_path("/usr/local"), "/usr/local")

    def test_roots_are_absolute_and_deduplicated_in_order(self) -> None:
        self.assertEqual(
            index.normalise_roots([self.home, self.home / "a" / "..", self.home]),
            [str(self.home)],
        )

    def test_walking_two_roots_finds_files_under_both(self) -> None:
        first, second = self.home / "Documents", self.home / "Downloads"
        first.mkdir()
        second.mkdir()
        (first / "one.tex").write_text("x")
        (second / "two.sty").write_text("x")
        stats = index.ScanStats()
        found = {r.path for r in index.walk_tree([first, second], stats)}
        self.assertEqual(found, {str(first / "one.tex"), str(second / "two.sty")})
        self.assertEqual(stats.roots, [str(first), str(second)])

    def test_a_file_reachable_from_two_roots_is_yielded_once(self) -> None:
        first, second = self.home / "Documents", self.home / "Downloads"
        first.mkdir()
        second.mkdir()
        (first / "shared.tex").write_text("x")
        os.symlink(first, second / "link-to-documents")
        stats = index.ScanStats()
        found = [r.path for r in index.walk_tree([first, second], stats)]
        self.assertEqual(found, [str(first / "shared.tex")])
        self.assertEqual(stats.files_found, 1)

    def test_summary_names_every_root(self) -> None:
        stats = index.ScanStats(roots=["/a", "/b"], finished=True)
        self.assertIn("Scan of /a, /b complete", stats.summary())

    def test_run_scan_defaults_to_the_default_roots(self) -> None:
        (self.home / "Documents").mkdir()
        (self.home / "Downloads").mkdir()
        (self.home / "Documents" / "paper.tex").write_text("x")
        (self.home / "Downloads" / "grabbed.sty").write_text("x")
        (self.home / "Elsewhere").mkdir()
        (self.home / "Elsewhere" / "ignored.tex").write_text("x")
        with self._as_home():
            stats = index.run_scan(db_path=self.home / "index.sqlite3")
        self.assertTrue(stats.complete)
        self.assertEqual(stats.files_found, 2)
        conn = index.connect(self.home / "index.sqlite3")
        self.addCleanup(conn.close)
        paths = {entry.path for entry in index.list_files(conn)}
        self.assertEqual(
            paths,
            {
                str(self.home / "Documents" / "paper.tex"),
                str(self.home / "Downloads" / "grabbed.sty"),
            },
        )


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

    def test_setting_a_description_reports_whether_it_landed(self) -> None:
        self.scan()
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        target = str(self.files["main"].resolve())
        self.assertTrue(index.set_description(conn, target, "kept"))
        # An uncatalogued path must not be silently accepted.
        self.assertFalse(index.set_description(conn, str(self.root / "ghost.tex"), "lost"))

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

    def test_filter_treats_like_wildcards_as_literal_text(self) -> None:
        """The filter is a substring match, so % and _ are not wildcards."""
        for name in ("100%done.tex", "a_b.tex", "axb.tex"):
            (self.root / "papers" / name).write_text("x")
        self.scan()
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        percent = index.list_files(conn, query="100%done")
        self.assertEqual([e.name for e in percent], ["100%done.tex"])
        underscore = index.list_files(conn, query="a_b.tex")
        self.assertEqual([e.name for e in underscore], ["a_b.tex"])
        # A lone % must not match everything.
        self.assertEqual(
            [e.name for e in index.list_files(conn, query="%")], ["100%done.tex"]
        )
        # A backslash is literal too, and must not break the ESCAPE clause.
        self.assertEqual(index.list_files(conn, query="\\"), [])

    def test_path_that_is_not_valid_utf8_is_skipped_not_fatal(self) -> None:
        """SQL text must be UTF-8; a surrogate path would crash the scan."""
        real = os.path.realpath
        target = str(self.files["main"].resolve())

        def surrogate(path: str) -> str:
            resolved = real(path)
            return resolved + "\udcff" if resolved == target else resolved

        stats = index.ScanStats()
        with unittest.mock.patch("os.path.realpath", surrogate):
            found = [r.path for r in index.walk_tree(self.root, stats)]
        self.assertNotIn(target + "\udcff", found)
        self.assertGreaterEqual(stats.skipped, 1)
        # The rest of the tree is still catalogued.
        self.assertIn(str(self.files["sty"].resolve()), found)

    def test_concurrent_connections_to_a_new_catalog_all_succeed(self) -> None:
        """`PRAGMA journal_mode = WAL` ignores busy_timeout and can fail."""
        import threading

        db = self.root / "contended.sqlite3"
        failures: list[str] = []

        def open_and_close() -> None:
            try:
                index.connect(db).close()
            except Exception as exc:  # pragma: no cover - the old bug
                failures.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=open_and_close) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])

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


class IgnoreTests(unittest.TestCase):
    """Ignoring hides a directory tree; it never deletes anything."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.files = build_fixture(self.root)
        self.db = self.root / "index.sqlite3"
        self.addCleanup(self.tmp.cleanup)
        index.run_scan(self.root, db_path=self.db)
        self.conn = index.connect(self.db)
        self.addCleanup(self.conn.close)
        self.papers = str(self.root / "papers")
        self.thesis = str(self.root / "papers" / "thesis")

    def visible(self) -> set[str]:
        return {entry.path for entry in index.list_files(self.conn)}

    def test_ignoring_hides_the_directory_and_everything_under_it(self) -> None:
        before = self.visible()
        self.assertTrue(index.ignore_directory(self.conn, self.papers))
        hidden = before - self.visible()
        self.assertEqual(
            hidden,
            {
                str(self.files[key].resolve())
                for key in ("main", "chapter", "other_main", "spaces", "upper")
            },
        )
        self.assertNotIn(
            self.papers, [path for path, _ in index.list_directories(self.conn)]
        )
        self.assertNotIn(
            self.thesis, [path for path, _ in index.list_directories(self.conn)]
        )

    def test_rows_and_descriptions_survive_being_ignored(self) -> None:
        target = str(self.files["main"].resolve())
        index.set_description(self.conn, target, "keep me")
        index.ignore_directory(self.conn, self.papers)
        self.assertNotIn(target, self.visible())
        entry = index.get_file(self.conn, target)
        assert entry is not None
        self.assertEqual(entry.description, "keep me")
        index.unignore_directory(self.conn, self.papers)
        self.assertIn(target, self.visible())
        restored = index.get_file(self.conn, target)
        assert restored is not None
        self.assertEqual(restored.description, "keep me")

    def test_counts_follow_the_ignore_rules(self) -> None:
        total = index.count_files(self.conn)
        index.ignore_directory(self.conn, self.papers)
        self.assertEqual(index.count_files(self.conn), total - 5)
        self.assertEqual(index.count_files(self.conn, include_ignored=True), total)

    def test_include_ignored_shows_everything_again(self) -> None:
        index.ignore_directory(self.conn, self.papers)
        paths = {
            entry.path
            for entry in index.list_files(self.conn, include_ignored=True)
        }
        self.assertIn(str(self.files["main"].resolve()), paths)

    def test_under_returns_a_whole_tree(self) -> None:
        paths = {
            entry.path
            for entry in index.list_files(
                self.conn, under=self.papers, include_ignored=True
            )
        }
        self.assertIn(str(self.files["main"].resolve()), paths)   # in a subdirectory
        self.assertIn(str(self.files["upper"].resolve()), paths)  # directly inside
        self.assertNotIn(str(self.files["sty"].resolve()), paths)

    def test_a_sibling_with_a_shared_prefix_is_not_hidden(self) -> None:
        self.assertIsNone(index.covering_ignore("/a/bc", ["/a/b"]))
        self.assertEqual(index.covering_ignore("/a/b/c", ["/a/b"]), "/a/b")
        self.assertEqual(index.covering_ignore("/a/b", ["/a/b"]), "/a/b")

    def test_ignoring_something_already_hidden_changes_nothing(self) -> None:
        self.assertTrue(index.ignore_directory(self.conn, self.papers))
        self.assertFalse(index.ignore_directory(self.conn, self.thesis))
        self.assertEqual(index.list_ignored(self.conn), [self.papers])

    def test_unignoring_a_directory_hidden_by_its_parent_reports_false(self) -> None:
        index.ignore_directory(self.conn, self.papers)
        self.assertFalse(index.unignore_directory(self.conn, self.thesis))
        self.assertEqual(index.list_ignored(self.conn), [self.papers])

    def test_the_walk_does_not_enter_an_ignored_tree(self) -> None:
        stats = index.ScanStats()
        found = {
            record.path
            for record in index.walk_tree(self.root, stats, ignored=[self.papers])
        }
        self.assertNotIn(str(self.files["main"].resolve()), found)
        self.assertIn(str(self.files["sty"].resolve()), found)
        self.assertEqual(stats.ignored, 1)
        self.assertIn("ignored directory", stats.summary())

    def test_a_scan_reads_the_ignore_list_from_the_catalog(self) -> None:
        index.ignore_directory(self.conn, self.papers)
        (self.root / "papers" / "new.tex").write_text("x")
        stats = index.run_scan(self.root, db_path=self.db)
        self.assertTrue(stats.complete)
        self.assertGreaterEqual(stats.ignored, 1)
        self.assertIsNone(
            index.get_file(self.conn, str(self.root / "papers" / "new.tex"))
        )

    def test_an_explicit_ignore_list_overrides_the_catalog(self) -> None:
        index.ignore_directory(self.conn, self.papers)
        stats = index.run_scan(self.root, db_path=self.db, ignored=[])
        self.assertEqual(stats.ignored, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
