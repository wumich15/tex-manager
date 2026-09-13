"""Directory/file browser and description editor.

Cached catalog rows appear immediately; one full scan then runs in a Textual
thread worker, which owns its own SQLite connection and reports progress
through messages. See https://textual.textualize.io/guide/workers/.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    Static,
)
from textual.widgets.data_table import CellDoesNotExist
from textual.widgets.option_list import Option, OptionDoesNotExist
from textual.worker import get_current_worker

from . import index

ALL_DIRECTORIES = "\0all"
HEADING_PREFIX = "\0dir:"

def display(text: str) -> str:
    """Make a name or description safe for a single-line table cell.

    File names may legally contain newlines and other control characters; the
    full, unaltered path is still what gets opened and shown in the detail area.
    """
    return "".join(
        character if character.isprintable() or character == " " else "\ufffd"
        for character in text
    )


NVIM_MISSING = (
    "Neovim was not found on PATH. Install it with `brew install neovim` "
    "(macOS) or your package manager, then try again."
)


# --------------------------------------------------------------------------
# Worker messages
# --------------------------------------------------------------------------

@dataclass
class ScanProgress(Message):
    files_found: int
    dirs_visited: int
    skipped: int
    current_dir: str


@dataclass
class ScanBatch(Message):
    """A batch of discoveries has been committed to the catalog."""

    count: int


@dataclass
class ScanFinished(Message):
    stats: index.ScanStats
    error: str | None = None


# --------------------------------------------------------------------------
# Description dialog
# --------------------------------------------------------------------------

class DescriptionDialog(ModalScreen[str | None]):
    """Small text dialog. Enter saves the description, Escape cancels."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, name_shown: str, description: str) -> None:
        super().__init__()
        self._name_shown = name_shown
        self._description = description

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Description for {self._name_shown}", id="dialog-title")
            yield Input(value=self._description, id="description-input")
            yield Label("Enter saves  ·  Escape cancels", id="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#description-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

class TexmanApp(App[None]):
    """Terminal catalog of local `.tex` and `.sty` files."""

    CSS_PATH = "tui.tcss"
    TITLE = "texman"

    BINDINGS = [
        Binding("slash", "focus_filter", "Filter", key_display="/"),
        Binding("enter", "open_file", "Open in Neovim", show=True),
        Binding("d", "edit_description", "Describe"),
        Binding("r", "rescan", "Rescan"),
        Binding("s", "stop_scan", "Stop scan"),
        Binding("q", "quit_app", "Quit"),
        Binding("escape", "leave_filter", "Leave filter", show=False),
    ]

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        scan_root: str = "/",
        autoscan: bool = True,
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._scan_root = scan_root
        self._autoscan = autoscan
        self._conn = None
        self._entries: dict[str, index.CatalogEntry] = {}
        self._row_order: list[str] = []
        self._selected_dir: str = ALL_DIRECTORIES
        self._filter: str = ""
        self._scan_running = False
        self._pending_refresh = False
        self._last_stats: index.ScanStats | None = None
        self._directory_signature: tuple[tuple[str, int], ...] = ()
        # Messages can arrive before the widgets exist and while the app is
        # being torn down; handlers must do nothing in either case.
        self._widgets_ready = False
        # A redraw of a whole-machine catalog costs a noticeable fraction of a
        # second, so refreshes during a scan are paced by what the last one
        # actually took instead of a fixed interval.
        self._reload_cost = 0.0
        self._last_reload_at = 0.0

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            yield OptionList(id="directories")
            with Vertical(id="right"):
                yield Input(placeholder="Filter path or description", id="filter")
                yield DataTable(id="files", cursor_type="row", zebra_stripes=True)
                yield Static("", id="empty-state")
                yield Static("", id="detail")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._conn = index.connect(self._db_path)
        table = self.query_one("#files", DataTable)
        table.add_column("Name", key="name", width=34)
        table.add_column("Ext", key="ext", width=5)
        table.add_column("Description", key="description")
        self.query_one("#filter", Input).display = False
        self._widgets_ready = True
        self.reload_catalog()
        table.focus()
        self.set_interval(0.5, self._apply_pending_refresh)
        if self._autoscan:
            self.start_scan()
        else:
            self._update_status("Scan not started. Press r to scan.")

    def on_unmount(self) -> None:
        self._widgets_ready = False
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------- rendering

    def reload_catalog(self, keep_path: str | None = None) -> None:
        """Redraw the directory list and file table from the catalog."""
        if not self._widgets_ready or self._conn is None:
            return
        started = time.monotonic()
        keep_path = keep_path or self.selected_path
        self._reload_directories()
        self._reload_files(keep_path)
        self._refresh_detail()
        self._reload_cost = time.monotonic() - started
        self._last_reload_at = time.monotonic()

    def _reload_directories(self) -> None:
        option_list = self.query_one("#directories", OptionList)
        directories = index.list_directories(self._conn)
        signature = tuple(directories)
        if signature == self._directory_signature and option_list.option_count:
            return  # Nothing new to show; rebuilding thousands of options is slow.
        self._directory_signature = signature
        total = sum(count for _, count in directories)
        previous = self._selected_dir
        option_list.clear_options()
        option_list.add_option(Option(f"All directories ({total})", id=ALL_DIRECTORIES))
        for path, count in directories:
            option_list.add_option(Option(f"{path} ({count})", id=path))
        known = {ALL_DIRECTORIES, *(path for path, _ in directories)}
        if previous not in known:
            self._selected_dir = ALL_DIRECTORIES
        try:
            option_list.highlighted = option_list.get_option_index(self._selected_dir)
        except OptionDoesNotExist:  # pragma: no cover - guarded by `known` above
            option_list.highlighted = 0

    def _reload_files(self, keep_path: str | None = None) -> None:
        table = self.query_one("#files", DataTable)
        parent = None if self._selected_dir == ALL_DIRECTORIES else self._selected_dir
        entries = index.list_files(self._conn, parent_dir=parent, query=self._filter or None)

        table.clear()
        self._entries = {entry.path: entry for entry in entries}
        self._row_order = []
        grouped = parent is None
        current_group: str | None = None
        for entry in entries:
            if grouped and entry.parent_dir != current_group:
                current_group = entry.parent_dir
                key = f"{HEADING_PREFIX}{current_group}"
                table.add_row(
                    Text(display(current_group), style="bold"),
                    Text(""),
                    Text(""),
                    key=key,
                )
                self._row_order.append(key)
            table.add_row(
                display(entry.name),
                entry.extension.lstrip("."),
                display(entry.description) or Text("—", style="dim"),
                key=entry.path,
            )
            self._row_order.append(entry.path)

        empty = self.query_one("#empty-state", Static)
        if entries:
            empty.display = False
            table.display = True
            if keep_path in self._entries:
                self._move_cursor_to(keep_path)
            else:
                self._move_cursor_to_first_file()
        else:
            table.display = False
            empty.display = True
            empty.update(self._empty_message())

    def _empty_message(self) -> str:
        if self._filter:
            return (
                f"Nothing matches “{self._filter}”.\n"
                "Edit the filter above, or empty it to see every file again."
            )
        if index.count_files(self._conn) == 0:
            return (
                "No .tex or .sty files catalogued yet.\n"
                "A scan is running — results appear as they are found. "
                "Press r to rescan, s to stop."
            )
        return "This directory has no matching files."

    def _move_cursor_to(self, path: str) -> None:
        table = self.query_one("#files", DataTable)
        try:
            row = self._row_order.index(path)
        except ValueError:
            return
        table.move_cursor(row=row)

    def _move_cursor_to_first_file(self) -> None:
        for offset, key in enumerate(self._row_order):
            if not key.startswith(HEADING_PREFIX):
                self.query_one("#files", DataTable).move_cursor(row=offset)
                return

    @property
    def selected_path(self) -> str | None:
        """Path of the highlighted file row, or None on a heading or empty table."""
        if not self._widgets_ready:
            return None
        table = self.query_one("#files", DataTable)
        row = table.cursor_row
        if row is None or not 0 <= row < len(self._row_order):
            return None
        key = self._row_order[row]
        return None if key.startswith(HEADING_PREFIX) else key

    @property
    def selected_entry(self) -> index.CatalogEntry | None:
        path = self.selected_path
        return self._entries.get(path) if path else None

    def _refresh_detail(self) -> None:
        if not self._widgets_ready:
            return
        detail = self.query_one("#detail", Static)
        entry = self.selected_entry
        if entry is None:
            detail.update(Text("No file selected.", style="dim"))
            return
        body = Text()
        body.append(entry.path + "\n", style="bold")
        if not entry.available:
            body.append("unavailable — the file is missing or not readable\n", style="red")
        body.append(f"{entry.extension}  ·  last seen {entry.last_seen}\n", style="dim")
        body.append(entry.description or "No description yet. Press d to add one.")
        detail.update(body)

    def _update_status(self, text: str) -> None:
        if not self._widgets_ready:
            return
        self.query_one("#status", Static).update(text)

    # ----------------------------------------------------------------- events

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if not self._widgets_ready or event.option_list.id != "directories":
            return
        chosen = event.option.id or ALL_DIRECTORIES
        if chosen != self._selected_dir:
            self._selected_dir = chosen
            self._reload_files()
            self._refresh_detail()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._refresh_detail()


    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open_file()

    def on_input_changed(self, event: Input.Changed) -> None:
        if not self._widgets_ready or event.input.id != "filter":
            return
        self._filter = event.value.strip()
        self._reload_files()
        self._refresh_detail()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            self.query_one("#files", DataTable).focus()

    # ------------------------------------------------------------- scanning

    def start_scan(self) -> None:
        """Start one full scan; overlapping scans are refused."""
        if self._scan_running:
            self.notify("A scan is already running.", severity="warning")
            return
        self._scan_running = True
        self._update_status(f"Scanning {self._scan_root} …")
        self._scan_worker(self._scan_root)

    @work(thread=True, group="scan", exclusive=True)
    def _scan_worker(self, root: str) -> None:
        worker = get_current_worker()

        def progress(stats: index.ScanStats) -> None:
            self.post_message(
                ScanProgress(
                    files_found=stats.files_found,
                    dirs_visited=stats.dirs_visited,
                    skipped=stats.skipped,
                    current_dir=stats.current_dir,
                )
            )

        def batch(records, stats: index.ScanStats) -> None:
            self.post_message(ScanBatch(count=len(records)))

        stats = index.ScanStats(root=root)
        try:
            stats = index.run_scan(
                root,
                db_path=self._db_path,
                should_cancel=lambda: worker.is_cancelled,
                on_progress=progress,
                on_batch=batch,
            )
        except Exception as exc:
            # A failing scan must not crash the app or leave it believing a scan
            # is still running; report it and let the user try again.
            self.post_message(
                ScanFinished(stats=stats, error=f"{type(exc).__name__}: {exc}")
            )
            return
        self.post_message(ScanFinished(stats=stats))

    def on_scan_progress(self, message: ScanProgress) -> None:
        self._update_status(
            f"Scanning: {message.files_found} files · {message.dirs_visited} dirs · "
            f"{message.skipped} skipped · {message.current_dir}"
        )

    def on_scan_batch(self, message: ScanBatch) -> None:
        self._pending_refresh = True

    def on_scan_finished(self, message: ScanFinished) -> None:
        self._scan_running = False
        self._last_stats = message.stats
        self._pending_refresh = False
        self.reload_catalog()
        if message.error is not None:
            self._update_status(f"Scan failed: {message.error}")
            self.notify(
                f"The scan stopped with an error: {message.error}. Everything "
                "already catalogued was kept; press r to try again.",
                severity="error",
                timeout=12,
            )
            return
        summary = message.stats.summary()
        if message.stats.skipped_samples:
            summary += f" · e.g. {message.stats.skipped_samples[0]}"
        self._update_status(summary)
        if not message.stats.complete:
            self.notify(
                "Scan stopped before finishing; the catalog is incomplete. "
                "Descriptions and discoveries so far were kept.",
                severity="warning",
            )
        elif message.stats.skipped:
            self.notify(
                f"{message.stats.skipped} path(s) were skipped. On macOS, grant "
                "your terminal Full Disk Access and rescan for fuller coverage.",
                severity="information",
            )

    def _apply_pending_refresh(self) -> None:
        """Redraw for newly discovered files, no faster than redrawing costs."""
        if not self._pending_refresh:
            return
        wait = max(0.5, self._reload_cost * 4)
        if time.monotonic() - self._last_reload_at < wait:
            return
        self._pending_refresh = False
        self.reload_catalog()

    # ------------------------------------------------------------- actions

    def _text_input_focused(self) -> bool:
        """Letter shortcuts must not fire while the user is typing."""
        return isinstance(self.focused, Input)

    def action_focus_filter(self) -> None:
        if self._text_input_focused():
            return
        filter_input = self.query_one("#filter", Input)
        filter_input.display = True
        filter_input.focus()

    def action_leave_filter(self) -> None:
        filter_input = self.query_one("#filter", Input)
        if not filter_input.has_focus:
            return
        if not filter_input.value:
            filter_input.display = False
        self.query_one("#files", DataTable).focus()

    def action_open_file(self) -> None:
        if self._text_input_focused():
            return
        entry = self.selected_entry
        if entry is None:
            return
        if not entry.available:
            self.notify(
                f"{entry.path} is unavailable; its description is kept.",
                severity="error",
            )
            return
        self._open_in_neovim(entry)

    def _open_in_neovim(self, entry: index.CatalogEntry) -> None:
        """Hand the terminal to Neovim, then restore the same selection."""
        keep = entry.path
        if shutil.which("nvim") is None:
            self.notify(NVIM_MISSING, severity="error", timeout=12)
            return
        # `App.suspend()` resumes application mode after the `with` body, but not
        # if the body raises, which would leave the terminal unusable. So the
        # subprocess failure is captured inside the block and reported after it.
        failure: OSError | None = None
        with self.suspend():
            try:
                subprocess.run(["nvim", entry.path], cwd=entry.parent_dir, check=False)
            except OSError as exc:
                failure = exc
        if failure is not None:
            if isinstance(failure, FileNotFoundError):
                self.notify(NVIM_MISSING, severity="error", timeout=12)
            else:
                self.notify(f"Could not start Neovim: {failure}", severity="error")
            return
        # Editing a file changes no catalogued field, so the table stands as it
        # is and the same row stays selected.
        table = self.query_one("#files", DataTable)
        self._move_cursor_to(keep)
        table.focus()
        self._refresh_detail()

    def action_edit_description(self) -> None:
        if self._text_input_focused():
            return
        entry = self.selected_entry
        if entry is None:
            return
        path = entry.path

        def save(value: str | None) -> None:
            if value is None:
                return
            description = value.strip()
            if not index.set_description(self._conn, path, description):
                self.notify(
                    f"{path} is no longer in the catalog, so the description was "
                    "not saved. Rescan with r and try again.",
                    severity="error",
                )
                return
            self._update_description_cell(path, description)

        self.push_screen(
            DescriptionDialog(display(entry.name), entry.description), save
        )

    def _update_description_cell(self, path: str, description: str) -> None:
        """Show a saved description at once, without redrawing the table."""
        entry = self._entries.get(path)
        if entry is None:
            self.reload_catalog(keep_path=path)
            return
        entry.description = description
        table = self.query_one("#files", DataTable)
        try:
            table.update_cell(
                path, "description", display(description) or Text("—", style="dim")
            )
        except CellDoesNotExist:  # pragma: no cover - the row was just rebuilt
            self.reload_catalog(keep_path=path)
            return
        self._refresh_detail()

    def action_rescan(self) -> None:
        if self._text_input_focused():
            return
        self.start_scan()

    def action_stop_scan(self) -> None:
        if self._text_input_focused():
            return
        if not self._scan_running:
            self.notify("No scan is running.", severity="information")
            return
        self.workers.cancel_group(self, "scan")
        self._update_status("Stopping scan …")

    def action_quit_app(self) -> None:
        if self._text_input_focused():
            return
        # Cancel the scan first, so quitting does not wait on the worker.
        self.workers.cancel_group(self, "scan")
        self.exit()


def run(db_path: str | os.PathLike[str] | None = None) -> None:  # pragma: no cover
    TexmanApp(db_path=db_path).run()
