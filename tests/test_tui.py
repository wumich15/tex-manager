"""Checks for the terminal UI, driven headlessly by Textual's test pilot.

Neovim is stubbed out, so no editor is launched and no scan touches anything
outside a temporary fixture.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import DataTable, Input, OptionList, Static

from texman import index
from texman.tui import (
    ALL_DIRECTORIES,
    DescriptionDialog,
    ScanFinished,
    ScanProgress,
    TexmanApp,
)
from tests.test_index import build_fixture


class TrackedSuspend:
    """Stands in for `App.suspend()`, recording how the block was left.

    The real `App.suspend()` resumes application mode after its body, but not if
    the body raises — an exception that escapes leaves the terminal unusable, so
    tests assert that none does.
    """

    def __init__(self) -> None:
        self.entered = 0
        self.escaped: BaseException | None = None

    def __call__(self):
        return self

    def __enter__(self):
        self.entered += 1
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.escaped = exc
        return False


class TuiTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.files = build_fixture(self.root)
        self.db = self.root / "index.sqlite3"
        self.addCleanup(self.tmp.cleanup)
        index.run_scan(self.root, db_path=self.db)
        # `suspend()` needs a real terminal; the headless driver has none.
        self.suspend = TrackedSuspend()
        patcher = mock.patch.object(
            TexmanApp, "suspend", lambda app: self.suspend()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, **kwargs) -> TexmanApp:
        options = dict(db_path=self.db, scan_root=str(self.root), autoscan=False)
        options.update(kwargs)
        return TexmanApp(**options)

    @staticmethod
    def table_paths(app: TexmanApp) -> list[str]:
        return [key for key in app._row_order if not key.startswith("\0dir:")]

    @staticmethod
    def static_text(app: TexmanApp, selector: str) -> str:
        content = app.query_one(selector, Static).content
        return content.plain if hasattr(content, "plain") else str(content)


class CatalogViewTests(TuiTestCase):
    async def test_shows_cached_rows_grouped_by_directory(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(len(self.table_paths(app)), len(self.files))
            headings = [k for k in app._row_order if k.startswith("\0dir:")]
            self.assertEqual(len(headings), len({f.parent for f in self.files.values()}))
            # Every file row follows its directory heading.
            self.assertTrue(app._row_order[0].startswith("\0dir:"))

    async def test_directory_list_offers_all_directories_first(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            options = app.query_one("#directories", OptionList)
            self.assertEqual(options.get_option_at_index(0).id, ALL_DIRECTORIES)
            self.assertEqual(options.option_count, 1 + len({f.parent for f in self.files.values()}))

    async def test_selecting_a_directory_restricts_the_table(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            thesis = str(self.root / "papers" / "thesis")
            options = app.query_one("#directories", OptionList)
            options.highlighted = options.get_option_index(thesis)
            await pilot.pause()
            paths = self.table_paths(app)
            self.assertEqual(len(paths), 2)
            self.assertTrue(all(p.startswith(thesis) for p in paths))
            # A single directory needs no headings.
            self.assertFalse([k for k in app._row_order if k.startswith("\0dir:")])

    async def test_detail_area_shows_full_path_and_description(self) -> None:
        target = str(self.files["main"].resolve())
        conn = index.connect(self.db)
        index.set_description(conn, target, "the thesis itself")
        conn.close()
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._move_cursor_to(target)
            await pilot.pause()
            text = self.static_text(app, "#detail")
            self.assertIn(target, text)
            self.assertIn("the thesis itself", text)

    async def test_missing_file_is_shown_as_unavailable(self) -> None:
        target = str(self.files["chapter"].resolve())
        os.unlink(target)
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._move_cursor_to(target)
            await pilot.pause()
            self.assertIn("unavailable", self.static_text(app, "#detail"))

    async def test_empty_catalog_shows_an_empty_state(self) -> None:
        empty_db = self.root / "empty.sqlite3"
        app = self.app(db_path=empty_db)
        async with app.run_test() as pilot:
            await pilot.pause()
            state = app.query_one("#empty-state", Static)
            self.assertTrue(state.display)
            self.assertFalse(app.query_one("#files", DataTable).display)
            self.assertIn("No .tex or .sty files", self.static_text(app, "#empty-state"))


class FilterTests(TuiTestCase):
    async def test_slash_focuses_the_filter_and_it_matches_substrings(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            filter_input = app.query_one("#filter", Input)
            self.assertTrue(filter_input.has_focus)
            filter_input.value = "NOTES WITH"
            await pilot.pause()
            self.assertEqual(len(self.table_paths(app)), 2)

    async def test_filter_matches_descriptions_case_insensitively(self) -> None:
        target = str(self.files["sty"].resolve())
        conn = index.connect(self.db)
        index.set_description(conn, target, "Shared MACROS")
        conn.close()
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            app.query_one("#filter", Input).value = "macros"
            await pilot.pause()
            self.assertEqual(self.table_paths(app), [target])

    async def test_letter_shortcuts_do_not_fire_while_typing(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            with mock.patch.object(app, "start_scan") as start_scan:
                await pilot.press("d", "r", "s", "q")
                await pilot.pause()
            start_scan.assert_not_called()
            self.assertEqual(app.query_one("#filter", Input).value, "drsq")
            self.assertIsInstance(app.screen, type(app.screen))
            self.assertNotIsInstance(app.screen, DescriptionDialog)
            self.assertTrue(app.is_running)

    async def test_escape_leaves_the_filter_without_clearing_it(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            app.query_one("#filter", Input).value = "thesis"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertTrue(app.query_one("#files", DataTable).has_focus)
            self.assertEqual(app._filter, "thesis")

    async def test_no_match_hint_describes_what_actually_works(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            app.query_one("#filter", Input).value = "zzzz-nothing"
            await pilot.pause()
            hint = self.static_text(app, "#empty-state")
            self.assertIn("Nothing matches", hint)
            # Escape leaves the filter; it does not clear it, so don't say so.
            self.assertNotIn("Escape", hint)


class DescriptionTests(TuiTestCase):
    async def test_saving_a_description_persists_and_refreshes(self) -> None:
        target = str(self.files["spaces"].resolve())
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._move_cursor_to(target)
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            self.assertIsInstance(app.screen, DescriptionDialog)
            app.screen.query_one(Input).value = "seminar notes"
            await pilot.press("enter")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, DescriptionDialog)
            self.assertEqual(app._entries[target].description, "seminar notes")

        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        entry = index.get_file(conn, target)
        assert entry is not None
        self.assertEqual(entry.description, "seminar notes")

    async def test_cancelling_the_dialog_leaves_the_description_unchanged(self) -> None:
        target = str(self.files["main"].resolve())
        conn = index.connect(self.db)
        index.set_description(conn, target, "original")
        conn.close()
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._move_cursor_to(target)
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            app.screen.query_one(Input).value = "discard me"
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app._entries[target].description, "original")

        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        entry = index.get_file(conn, target)
        assert entry is not None
        self.assertEqual(entry.description, "original")

    async def test_unsaveable_description_is_reported_not_swallowed(self) -> None:
        target = str(self.files["main"].resolve())
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._move_cursor_to(target)
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            app.screen.query_one(Input).value = "will not land"
            with mock.patch.object(index, "set_description", return_value=False):
                with mock.patch.object(app, "notify") as notify:
                    await pilot.press("enter")
                    await pilot.pause()
            self.assertIn("was not saved", notify.call_args[0][0])

    async def test_description_editing_keeps_the_selection(self) -> None:
        target = str(self.files["sty"].resolve())
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._move_cursor_to(target)
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            app.screen.query_one(Input).value = "package"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.selected_path, target)


class OpenInNeovimTests(TuiTestCase):
    async def test_enter_runs_nvim_with_the_path_as_one_argument(self) -> None:
        target = str(self.files["spaces"].resolve())
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._move_cursor_to(target)
            await pilot.pause()
            with mock.patch.object(subprocess, "run") as run:
                await pilot.press("enter")
                await pilot.pause()
            run.assert_called_once()
            self.assertIsNone(self.suspend.escaped)
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["nvim", target])
            self.assertEqual(kwargs["cwd"], os.path.dirname(target))
            self.assertIn(" ", target)  # the fixture path really has spaces

    async def test_selection_is_restored_after_the_editor_exits(self) -> None:
        target = str(self.files["other_main"].resolve())
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._move_cursor_to(target)
            await pilot.pause()
            with mock.patch.object(subprocess, "run"):
                await pilot.press("enter")
                await pilot.pause()
            self.assertEqual(app.selected_path, target)
            self.assertTrue(app.query_one("#files", DataTable).has_focus)

    async def test_missing_neovim_is_explained_without_suspending(self) -> None:
        """`App.suspend()` does not restore the terminal if its body raises."""
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            with mock.patch.object(shutil, "which", return_value=None):
                with mock.patch.object(subprocess, "run") as run:
                    with mock.patch.object(app, "notify") as notify:
                        await pilot.press("enter")
                        await pilot.pause()
            run.assert_not_called()
            self.assertEqual(self.suspend.entered, 0)  # never handed the terminal over
            notify.assert_called_once()
            self.assertIn("Install it", notify.call_args[0][0])

    async def test_editor_failure_is_reported_after_resuming(self) -> None:
        """A subprocess error must be raised inside, not through, suspend()."""
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            with mock.patch.object(subprocess, "run", side_effect=OSError("boom")):
                with mock.patch.object(app, "notify") as notify:
                    await pilot.press("enter")
                    await pilot.pause()
            self.assertEqual(self.suspend.entered, 1)
            # Nothing propagated out of the suspend block, so the terminal was
            # handed back before the error was shown.
            self.assertIsNone(self.suspend.escaped)
            self.assertIn("Could not start Neovim", notify.call_args[0][0])

    async def test_unavailable_file_is_not_opened(self) -> None:
        target = str(self.files["chapter"].resolve())
        os.unlink(target)
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._move_cursor_to(target)
            await pilot.pause()
            with mock.patch.object(subprocess, "run") as run:
                with mock.patch.object(app, "notify") as notify:
                    await pilot.press("enter")
                    await pilot.pause()
            run.assert_not_called()
            self.assertIn("unavailable", notify.call_args[0][0])

    async def test_heading_rows_are_not_openable(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#files", DataTable).move_cursor(row=0)  # a heading row
            await pilot.pause()
            self.assertIsNone(app.selected_path)
            with mock.patch.object(subprocess, "run") as run:
                await pilot.press("enter")
                await pilot.pause()
            run.assert_not_called()


class ScanControlTests(TuiTestCase):
    async def test_background_scan_populates_and_reports(self) -> None:
        empty_db = self.root / "fresh.sqlite3"
        app = self.app(db_path=empty_db, autoscan=True)
        async with app.run_test() as pilot:
            for _ in range(200):
                await pilot.pause()
                if not app._scan_running and app._last_stats is not None:
                    break
            assert app._last_stats is not None
            self.assertTrue(app._last_stats.complete)
            self.assertEqual(len(self.table_paths(app)), len(self.files))
            self.assertIn("complete", self.static_text(app, "#status"))

    async def test_overlapping_scans_are_refused(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._scan_running = True
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("r")
                await pilot.pause()
            self.assertIn("already running", notify.call_args[0][0])

    async def test_stopping_without_a_scan_says_so(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("s")
                await pilot.pause()
            self.assertIn("No scan is running", notify.call_args[0][0])

    async def test_stopping_a_scan_keeps_descriptions(self) -> None:
        target = str(self.files["main"].resolve())
        conn = index.connect(self.db)
        index.set_description(conn, target, "keep me")
        conn.close()
        app = self.app(autoscan=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            for _ in range(200):
                await pilot.pause()
                if not app._scan_running:
                    break
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        entry = index.get_file(conn, target)
        assert entry is not None
        self.assertEqual(entry.description, "keep me")

    async def test_quit_exits(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            self.assertFalse(app.is_running)

    async def test_quit_during_a_scan_keeps_descriptions(self) -> None:
        target = str(self.files["main"].resolve())
        conn = index.connect(self.db)
        index.set_description(conn, target, "written before quitting")
        conn.close()
        app = self.app(autoscan=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            self.assertFalse(app.is_running)
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        entry = index.get_file(conn, target)
        assert entry is not None
        self.assertEqual(entry.description, "written before quitting")


class ScanFailureTests(TuiTestCase):
    async def test_a_failing_scan_is_reported_and_can_be_retried(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            with mock.patch.object(
                index, "run_scan", side_effect=RuntimeError("disk on fire")
            ):
                with mock.patch.object(app, "notify") as notify:
                    app.start_scan()
                    for _ in range(200):
                        await pilot.pause()
                        if not app._scan_running:
                            break
            self.assertFalse(app._scan_running)  # not wedged
            self.assertIn("disk on fire", notify.call_args[0][0])
            self.assertIn("Scan failed", self.static_text(app, "#status"))
            # A second scan is allowed once the first has failed.
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("r")
                await pilot.pause()
            notify.assert_not_called()


class TeardownTests(TuiTestCase):
    async def test_late_messages_after_teardown_are_ignored(self) -> None:
        """Widgets are gone once the app exits; late events must not raise."""
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
        self.assertFalse(app._widgets_ready)
        app._refresh_detail()
        app._update_status("late update")
        app.reload_catalog()
        self.assertIsNone(app.selected_path)

    async def test_scan_finishing_after_teardown_is_ignored(self) -> None:
        app = self.app(autoscan=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            stats = index.ScanStats(root=str(self.root), finished=True)
        # The worker's last messages can land after the app has stopped.
        app.on_scan_progress(ScanProgress(1, 1, 0, "/somewhere"))
        app.on_scan_finished(ScanFinished(stats=stats))


class NoApiDependencyTests(unittest.TestCase):
    def test_browsing_needs_neither_openai_nor_a_key(self) -> None:
        """Importing and using the catalog must not import the OpenAI client."""
        script = (
            "import sys, texman.cli, texman.tui, texman.index;"
            "assert 'openai' not in sys.modules, sorted(sys.modules);"
            "print('ok')"
        )
        env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "TEXMAN_OPENAI_MODEL")}
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
