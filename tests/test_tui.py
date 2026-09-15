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

from texman import documents, index
from texman.tui import (
    ALL_DIRECTORIES,
    HEADING_PREFIX,
    IGNORED_SEPARATOR,
    display,
    DescriptionDialog,
    NewDocumentDialog,
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
        # The template lives outside the scanned tree, as it does for real.
        self.config = tempfile.TemporaryDirectory()
        self.addCleanup(self.config.cleanup)
        self.preamble = Path(self.config.name).resolve() / "preamble.tex"
        index.run_scan(self.root, db_path=self.db)
        # `suspend()` needs a real terminal; the headless driver has none.
        self.suspend = TrackedSuspend()
        patcher = mock.patch.object(
            TexmanApp, "suspend", lambda app: self.suspend()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, **kwargs) -> TexmanApp:
        options = dict(
            db_path=self.db,
            scan_roots=[str(self.root)],
            autoscan=False,
            preamble_path=self.preamble,
        )
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


class IgnoreTests(TuiTestCase):
    """`i` hides a directory tree from the catalog, and shows it again."""

    def thesis_dir(self) -> str:
        return str(self.files["main"].parent)

    def papers_dir(self) -> str:
        return str(self.files["main"].parent.parent)

    async def highlight_directory(self, pilot, app, path: str) -> None:
        """Select a directory in the left pane, the way the user does."""
        option_list = app.query_one(OptionList)
        option_list.focus()
        option_list.highlighted = option_list.get_option_index(path)
        await pilot.pause()

    def listed_directories(self, app) -> list[str]:
        return [option.id for option in app.query_one(OptionList).options]

    async def test_i_hides_the_directory_of_the_selected_file(self) -> None:
        target = str(self.files["main"].resolve())
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._move_cursor_to(target)
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            self.assertEqual(app._ignored, [self.thesis_dir()])
            self.assertNotIn(target, self.table_paths(app))
            listed = self.listed_directories(app)
            above = listed[: listed.index(IGNORED_SEPARATOR)]
            self.assertNotIn(self.thesis_dir(), above)
            self.assertIn(self.thesis_dir(), listed)

    async def test_pressing_i_on_an_ignored_entry_restores_it(self) -> None:
        target = str(self.files["main"].resolve())
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._move_cursor_to(target)
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            await self.highlight_directory(pilot, app, self.thesis_dir())
            await pilot.press("i")
            await pilot.pause()
            self.assertEqual(app._ignored, [])
            self.assertIn(target, self.table_paths(app))

    async def test_ignoring_hides_subdirectories_too(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await self.highlight_directory(pilot, app, self.papers_dir())
            await pilot.press("i")
            await pilot.pause()
            self.assertEqual(app._ignored, [self.papers_dir()])
            self.assertEqual(app._selected_dir, ALL_DIRECTORIES)
            for key in ("main", "chapter", "spaces", "upper"):
                self.assertNotIn(str(self.files[key].resolve()), self.table_paths(app))
            self.assertIn(str(self.files["sty"].resolve()), self.table_paths(app))

    async def test_descriptions_survive_being_ignored(self) -> None:
        target = str(self.files["main"].resolve())
        conn = index.connect(self.db)
        index.set_description(conn, target, "the thesis")
        conn.close()
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._move_cursor_to(target)
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            await self.highlight_directory(pilot, app, self.thesis_dir())
            await pilot.press("i")
            await pilot.pause()
            self.assertEqual(app._entries[target].description, "the thesis")

    async def test_selecting_an_ignored_directory_shows_what_it_hides(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await self.highlight_directory(pilot, app, self.papers_dir())
            await pilot.press("i")
            await pilot.pause()
            await self.highlight_directory(pilot, app, self.papers_dir())
            # Files in subdirectories of the ignored tree, not just its own.
            self.assertIn(str(self.files["main"].resolve()), self.table_paths(app))
            self.assertIn(str(self.files["upper"].resolve()), self.table_paths(app))

    async def test_a_directory_hidden_by_its_parent_says_so(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await self.highlight_directory(pilot, app, self.papers_dir())
            await pilot.press("i")
            await pilot.pause()
            app._selected_dir = self.thesis_dir()
            with mock.patch.object(app, "notify") as notify:
                app.action_toggle_ignore()
            self.assertIn("hidden by", notify.call_args[0][0])
            self.assertEqual(app._ignored, [self.papers_dir()])

    async def test_i_does_nothing_while_the_filter_has_focus(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            self.assertEqual(app._ignored, [])
            self.assertEqual(app.query_one("#filter", Input).value, "i")

    async def test_a_rescan_does_not_re_add_an_ignored_directory(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await self.highlight_directory(pilot, app, self.papers_dir())
            await pilot.press("i")
            await pilot.pause()
        stats = index.run_scan(self.root, db_path=self.db)
        self.assertTrue(stats.complete)
        self.assertGreaterEqual(stats.ignored, 1)
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual([entry.path for entry in index.list_files(conn)],
                         sorted(
                             [str(self.files["hidden"].resolve()),
                              str(self.files["mixed_sty"].resolve()),
                              str(self.files["sty"].resolve())],
                             key=str.lower,
                         ))


class NavigationTests(TuiTestCase):
    """Vim-style motions: counts, `gg`/`G`, `;s`/`;a`, and `h`/`l`."""

    @staticmethod
    def headings(app) -> list[int]:
        return [i for i, key in enumerate(app._row_order) if key.startswith(HEADING_PREFIX)]

    @staticmethod
    def row(app) -> int:
        return app.query_one("#files", DataTable).cursor_row

    async def test_count_then_j_moves_that_many_rows(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            start = self.row(app)
            await pilot.press("5", "j")
            await pilot.pause()
            self.assertEqual(self.row(app), start + 5)
            await pilot.press("3", "k")
            await pilot.pause()
            self.assertEqual(self.row(app), start + 2)

    async def test_plain_j_and_k_move_one_row(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            start = self.row(app)
            await pilot.press("j", "j", "k")
            await pilot.pause()
            self.assertEqual(self.row(app), start + 1)

    async def test_count_applies_to_arrow_keys(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            start = self.row(app)
            await pilot.press("4", "down")
            await pilot.pause()
            self.assertEqual(self.row(app), start + 4)
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(self.row(app), start + 5)

    async def test_movement_stops_at_the_ends(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            last = len(app._row_order) - 1
            await pilot.press("9", "9", "j")
            await pilot.pause()
            self.assertEqual(self.row(app), last)
            await pilot.press("9", "9", "k")
            await pilot.pause()
            self.assertEqual(self.row(app), 0)

    async def test_gg_G_and_counted_G(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("G")
            await pilot.pause()
            self.assertEqual(self.row(app), len(app._row_order) - 1)
            await pilot.press("g", "g")
            await pilot.pause()
            self.assertEqual(self.row(app), 0)
            await pilot.press("4", "G")
            await pilot.pause()
            self.assertEqual(self.row(app), 3)
            await pilot.press("2", "g", "g")
            await pilot.pause()
            self.assertEqual(self.row(app), 1)

    async def test_semicolon_s_jumps_to_the_next_directory_group(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            headings = self.headings(app)
            self.assertGreater(len(headings), 2)
            self.assertEqual(self.row(app), headings[0] + 1)
            await pilot.press("semicolon", "s")
            await pilot.pause()
            self.assertEqual(self.row(app), headings[1] + 1)
            await pilot.press("2", "semicolon", "s")
            await pilot.pause()
            self.assertEqual(self.row(app), headings[3] + 1)
            await pilot.press("semicolon", "a")
            await pilot.pause()
            self.assertEqual(self.row(app), headings[2] + 1)

    async def test_semicolon_s_at_the_last_directory_says_so(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("G")
            await pilot.pause()
            before = self.row(app)
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("semicolon", "s")
                await pilot.pause()
            self.assertEqual(self.row(app), before)
            self.assertIn("No next directory", notify.call_args[0][0])

    async def test_semicolon_s_never_stops_a_scan(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            with mock.patch.object(app, "action_stop_scan") as stop:
                await pilot.press("semicolon", "s")
                await pilot.pause()
            stop.assert_not_called()

    async def test_semicolon_s_steps_the_directory_list_in_a_single_directory_view(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            options = app.query_one("#directories", OptionList)
            options.highlighted = 1
            await pilot.pause()
            first = app._selected_dir
            self.assertNotEqual(first, ALL_DIRECTORIES)
            self.assertEqual(self.headings(app), [])
            await pilot.press("semicolon", "s")
            await pilot.pause()
            self.assertEqual(options.highlighted, 2)
            self.assertNotEqual(app._selected_dir, first)
            self.assertNotEqual(app._selected_dir, ALL_DIRECTORIES)
            await pilot.press("semicolon", "a")
            await pilot.pause()
            self.assertEqual(app._selected_dir, first)

    async def test_h_and_l_switch_panes_and_counts_work_there_too(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            options = app.query_one("#directories", OptionList)
            self.assertTrue(options.has_focus)
            await pilot.press("2", "j")
            await pilot.pause()
            self.assertEqual(options.highlighted, 2)
            await pilot.press("G")
            await pilot.pause()
            self.assertEqual(options.highlighted, options.option_count - 1)
            await pilot.press("l")
            await pilot.pause()
            self.assertTrue(app.query_one("#files", DataTable).has_focus)

    async def test_directory_motions_skip_the_ignored_separator(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("i")  # ignore the selected file's directory
            await pilot.pause()
            options = app.query_one("#directories", OptionList)
            separator = options.get_option_index(IGNORED_SEPARATOR)
            await pilot.press("h")
            await pilot.pause()
            options.highlighted = separator - 1
            await pilot.pause()
            await pilot.press("j")
            await pilot.pause()
            self.assertEqual(options.highlighted, separator + 1)

    async def test_digits_and_motions_type_into_the_filter(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            start = self.row(app)
            await pilot.press("slash", "5", "j", "G", "semicolon", "s")
            await pilot.pause()
            self.assertEqual(app.query_one("#filter", Input).value, "5jG;s")
            self.assertEqual(app._count, "")
            self.assertEqual(app._prefix, "")
            # The text filtered the table; leaving the filter restores the rows
            # with the cursor where it was, so no motion ran.
            await pilot.press("escape")
            app.query_one("#filter", Input).value = ""
            await pilot.pause()
            self.assertEqual(self.row(app), start)

    async def test_unknown_sequence_is_reported_and_does_nothing(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("semicolon", "d")
                await pilot.pause()
            self.assertNotIsInstance(app.screen, DescriptionDialog)
            self.assertIn(";d", notify.call_args[0][0])

    async def test_count_is_dropped_by_a_non_motion_key(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("5", "d")
            await pilot.pause()
            self.assertIsInstance(app.screen, DescriptionDialog)
            self.assertEqual(app._count, "")
            await pilot.press("escape")
            await pilot.pause()

    async def test_escape_cancels_a_pending_sequence(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            start = self.row(app)
            await pilot.press("3", "semicolon", "escape", "j")
            await pilot.pause()
            self.assertEqual(app._prefix, "")
            self.assertEqual(self.row(app), start + 1)


class PreambleTests(TuiTestCase):
    async def test_p_creates_the_template_and_opens_it(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            before = app.selected_path
            with mock.patch.object(subprocess, "run") as run:
                with mock.patch.object(app, "notify") as notify:
                    await pilot.press("p")
                    await pilot.pause()
            self.assertTrue(self.preamble.is_file())
            self.assertIn("\\documentclass", self.preamble.read_text())
            self.assertIn("Created", notify.call_args[0][0])
            run.assert_called_once()
            self.assertEqual(run.call_args[0][0], ["nvim", str(self.preamble)])
            self.assertEqual(run.call_args[1]["cwd"], str(self.preamble.parent))
            self.assertIsNone(self.suspend.escaped)
            self.assertEqual(app.selected_path, before)
            self.assertTrue(app.query_one("#files", DataTable).has_focus)

    async def test_p_never_overwrites_an_existing_template(self) -> None:
        self.preamble.parent.mkdir(parents=True, exist_ok=True)
        self.preamble.write_text("% my preamble\n")
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            with mock.patch.object(subprocess, "run") as run:
                await pilot.press("p")
                await pilot.pause()
            self.assertEqual(self.preamble.read_text(), "% my preamble\n")
            run.assert_called_once()

    async def test_p_types_into_the_filter(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash", "p")
            await pilot.pause()
            self.assertEqual(app.query_one("#filter", Input).value, "p")
            self.assertFalse(self.preamble.exists())


class NewDocumentTests(TuiTestCase):
    def write_template(self) -> None:
        self.preamble.parent.mkdir(parents=True, exist_ok=True)
        self.preamble.write_text("\\documentclass{book}\n\\usepackage{mine}\n")

    async def test_n_creates_a_document_beside_the_selected_file(self) -> None:
        self.write_template()
        target = str(self.files["main"].resolve())
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._move_cursor_to(target)
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            self.assertIsInstance(app.screen, NewDocumentDialog)
            directory = app.screen.query_one("#document-directory", Input)
            self.assertEqual(directory.value, os.path.dirname(target))
            app.screen.query_one("#document-name", Input).value = "draft"
            with mock.patch.object(subprocess, "run") as run:
                await pilot.press("enter")
                await pilot.pause()
            created = os.path.join(os.path.dirname(target), "draft.tex")
            self.assertTrue(os.path.isfile(created))
            text = Path(created).read_text()
            self.assertTrue(text.startswith("\\documentclass{book}\n\\usepackage{mine}\n"))
            self.assertIn("\\begin{document}", text)
            # Catalogued at once, selected, and opened.
            self.assertIn(created, app._entries)
            self.assertEqual(app.selected_path, created)
            self.assertEqual(run.call_args[0][0], ["nvim", created])
            self.assertEqual(run.call_args[1]["cwd"], os.path.dirname(created))
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        self.assertIsNotNone(index.get_file(conn, created))

    async def test_the_directory_field_wins_and_is_created_if_missing(self) -> None:
        self.write_template()
        wanted = self.root / "brand new" / "project"
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#document-name", Input).value = "paper.tex"
            app.screen.query_one("#document-directory", Input).value = str(wanted)
            with mock.patch.object(subprocess, "run"):
                await pilot.press("enter")
                await pilot.pause()
            created = str(wanted / "paper.tex")
            self.assertTrue(os.path.isfile(created))
            self.assertEqual(app.selected_path, created)
            self.assertIn(str(wanted), [o.id for o in app.query_one(OptionList).options])

    async def test_without_a_template_the_default_is_used_and_said(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            self.assertIn("No preamble.tex yet", app.screen._template_note)
            app.screen.query_one("#document-name", Input).value = "fresh"
            with mock.patch.object(subprocess, "run"):
                with mock.patch.object(app, "notify") as notify:
                    await pilot.press("enter")
                    await pilot.pause()
            self.assertIn("built-in", notify.call_args[0][0])
            created = app.selected_path
            assert created is not None
            self.assertTrue(created.endswith("fresh.tex"))
            self.assertIn(documents.DEFAULT_PREAMBLE, Path(created).read_text())

    async def test_an_existing_file_is_never_overwritten(self) -> None:
        target = self.files["main"].resolve()
        target.write_text("precious\n")
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._move_cursor_to(str(target))
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#document-name", Input).value = "main"
            with mock.patch.object(subprocess, "run") as run:
                with mock.patch.object(app, "notify") as notify:
                    await pilot.press("enter")
                    await pilot.pause()
            run.assert_not_called()
            self.assertIn("already exists", notify.call_args[0][0])
            self.assertEqual(target.read_text(), "precious\n")

    async def test_escape_creates_nothing(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            listed = set(self.table_paths(app))
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#document-name", Input).value = "never"
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, NewDocumentDialog)
            self.assertEqual(set(self.table_paths(app)), listed)
            self.assertFalse(list(self.root.rglob("never.tex")))

    async def test_enter_without_a_name_keeps_the_dialog_open(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, NewDocumentDialog)
            await pilot.press("escape")
            await pilot.pause()

    async def test_a_separator_in_the_name_is_refused(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#document-name", Input).value = "sub/dir.tex"
            with mock.patch.object(subprocess, "run") as run:
                with mock.patch.object(app, "notify") as notify:
                    await pilot.press("enter")
                    await pilot.pause()
            run.assert_not_called()
            self.assertIn("path separator", notify.call_args[0][0])

    async def test_n_types_into_the_filter(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash", "n")
            await pilot.pause()
            self.assertEqual(app.query_one("#filter", Input).value, "n")
            self.assertNotIsInstance(app.screen, NewDocumentDialog)


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
            stats = index.ScanStats(roots=[str(self.root)], finished=True)
        # The worker's last messages can land after the app has stopped.
        app.on_scan_progress(ScanProgress(1, 1, 0, "/somewhere"))
        app.on_scan_finished(ScanFinished(stats=stats))


class ScanRootTests(unittest.TestCase):
    """The app scans ~/Documents and ~/Downloads unless given roots."""

    def test_defaults_to_documents_and_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            (home / "Documents").mkdir()
            (home / "Downloads").mkdir()
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                app = TexmanApp(db_path=home / "index.sqlite3", autoscan=False)
                self.assertEqual(
                    list(app._scan_roots),
                    [str(home / "Documents"), str(home / "Downloads")],
                )
                self.assertEqual(app._roots_label(), "~/Documents, ~/Downloads")

    def test_given_roots_win(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            app = TexmanApp(
                db_path=home / "index.sqlite3",
                scan_roots=[str(home)],
                autoscan=False,
            )
            self.assertEqual(list(app._scan_roots), [str(home)])


class DisplayTests(unittest.TestCase):
    def test_control_characters_are_replaced_for_table_cells(self) -> None:
        self.assertEqual(display("two\nlines.tex"), "two\ufffdlines.tex")
        self.assertEqual(display("tab\there"), "tab\ufffdhere")
        self.assertEqual(display("ordinary name.tex"), "ordinary name.tex")
        self.assertEqual(display("accentué.tex"), "accentué.tex")


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
