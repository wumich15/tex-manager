"""Filesystem traversal and SQLite persistence for the texman catalog.

The catalog records where `.tex` and `.sty` files live; it never moves or
copies them, so relative `\\input`, image, and style references keep working.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence

MATCHED_EXTENSIONS = (".tex", ".sty")

# Virtual or auto-mounting trees that contain no real documents. Kept
# deliberately short: caches, hidden folders, and system library trees are
# scanned because they can legitimately contain TeX files.
EXCLUDED_ROOTS = (
    "/dev",
    "/proc",
    "/sys",
    "/net",   # macOS autofs trigger; touching it can block for a long time
    "/home",  # macOS autofs trigger
)

DEFAULT_BATCH_SIZE = 200
BUSY_TIMEOUT_MS = 5000
MAX_SKIPPED_SAMPLES = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    parent_dir TEXT NOT NULL,
    extension TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS files_parent_dir ON files (parent_dir);
"""


# --------------------------------------------------------------------------
# Locations and connections
# --------------------------------------------------------------------------

def data_dir() -> Path:
    """Return the per-user data directory, honouring XDG_DATA_HOME."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return Path(base) / "texman"


def default_db_path() -> Path:
    """Return the SQLite database path, which lives outside the repository."""
    override = os.environ.get("TEXMAN_DB")
    if override:
        return Path(override)
    return data_dir() / "index.sqlite3"


def connect(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Open (and initialise) a connection owned by the calling thread."""
    path = Path(db_path) if db_path is not None else default_db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    _enable_wal(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch to WAL if it is not already on, tolerating a concurrent switch.

    Unlike ordinary statements, `PRAGMA journal_mode = WAL` does not honour
    `busy_timeout`: it needs an exclusive lock and fails immediately if another
    connection is converting the same new file. The mode is a property of the
    file, so whoever wins sets it for everyone, and the default journal is
    correct in the meantime -- a loss here must not fail the caller.
    """
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() == "wal":
        return
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FileRecord:
    """A discovered file: identity plus the fields a scan may refresh."""

    path: str
    parent_dir: str
    extension: str


@dataclass
class CatalogEntry:
    """A row as the user interface consumes it."""

    path: str
    parent_dir: str
    extension: str
    description: str
    last_seen: str

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def available(self) -> bool:
        return os.path.exists(self.path)


@dataclass
class ScanStats:
    """Outcome of one traversal. A stopped scan is incomplete, not successful."""

    root: str = "/"
    files_found: int = 0
    dirs_visited: int = 0
    skipped: int = 0
    skipped_samples: list[str] = field(default_factory=list)
    excluded_roots: list[str] = field(default_factory=list)
    cancelled: bool = False
    finished: bool = False
    current_dir: str = ""

    @property
    def complete(self) -> bool:
        return self.finished and not self.cancelled

    def summary(self) -> str:
        state = "complete" if self.complete else (
            "stopped" if self.cancelled else "incomplete"
        )
        details = [f"{self.files_found} file(s) in {self.dirs_visited} directories"]
        if self.skipped:
            details.append(f"{self.skipped} path(s) skipped (permissions or removed)")
        if self.excluded_roots:
            details.append("excluded: " + ", ".join(self.excluded_roots))
        return f"Scan of {self.root} {state}: " + "; ".join(details)

    def note_skip(self, path: str) -> None:
        self.skipped += 1
        if len(self.skipped_samples) < MAX_SKIPPED_SAMPLES:
            self.skipped_samples.append(path)


# --------------------------------------------------------------------------
# Traversal
# --------------------------------------------------------------------------

def matched_extension(name: str) -> str | None:
    """Return the matched extension, lowercased, or None.

    Matching is case-insensitive and does not use `os.path.splitext`, which
    reports no extension at all for a name that is only a suffix, such as the
    literal file name `.tex`.
    """
    lowered = name.lower()
    for extension in MATCHED_EXTENSIONS:
        if lowered.endswith(extension):
            return extension
    return None


def matches(name: str) -> bool:
    """Match `.tex` and `.sty` case-insensitively."""
    return matched_extension(name) is not None


def _excluded_for(root: str) -> list[str]:
    """Excluded roots that actually sit underneath the requested root."""
    root = os.path.abspath(root)
    return [
        item
        for item in EXCLUDED_ROOTS
        if os.path.isdir(item) and (item == root or item.startswith(root.rstrip("/") + "/"))
    ]


def walk_tree(
    root: str | os.PathLike[str],
    stats: ScanStats,
    *,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[ScanStats], None] | None = None,
    progress_interval: float = 0.25,
) -> Iterator[FileRecord]:
    """Yield matching files under `root`, inspecting names and metadata only.

    Directory symlinks are not followed and device/inode pairs are remembered,
    so filesystem aliases are visited once. Permission errors and entries that
    vanish mid-walk are counted and skipped rather than raised.
    """
    root = os.path.abspath(os.fspath(root))
    stats.root = root
    excluded = set(_excluded_for(root))
    stats.excluded_roots = sorted(excluded)

    visited: set[tuple[int, int]] = set()
    seen_files: set[str] = set()
    pending: list[str] = [root]
    last_progress = 0.0

    while pending:
        if should_cancel is not None and should_cancel():
            stats.cancelled = True
            return
        directory = pending.pop()
        if directory in excluded:
            continue
        try:
            key = os.stat(directory, follow_symlinks=False)
        except OSError:
            stats.note_skip(directory)
            continue
        if not stat.S_ISDIR(key.st_mode):
            stats.note_skip(directory)
            continue
        identity = (key.st_dev, key.st_ino)
        if identity in visited:
            continue
        visited.add(identity)

        stats.current_dir = directory
        stats.dirs_visited += 1
        now = time.monotonic()
        if on_progress is not None and now - last_progress >= progress_interval:
            last_progress = now
            on_progress(stats)

        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                            continue
                        extension = matched_extension(entry.name)
                        if extension is None:
                            continue
                        # follow_symlinks=True so a link to a real file counts
                        # and a broken link quietly does not.
                        if not entry.is_file(follow_symlinks=True):
                            continue
                        canonical = os.path.realpath(entry.path)
                        try:
                            # A name that is not valid UTF-8 (possible on Linux)
                            # cannot be stored as SQL text; count it honestly
                            # instead of failing the scan.
                            canonical.encode("utf-8")
                        except UnicodeEncodeError:
                            stats.note_skip(entry.path.encode(
                                "utf-8", "replace").decode("utf-8"))
                            continue
                        if canonical in seen_files:
                            continue
                        seen_files.add(canonical)
                        stats.files_found += 1
                        yield FileRecord(
                            path=canonical,
                            parent_dir=os.path.dirname(canonical),
                            extension=extension,
                        )
                    except OSError:
                        stats.note_skip(entry.path)
        except OSError:
            stats.note_skip(directory)

    stats.current_dir = ""


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_files(conn: sqlite3.Connection, records: Sequence[FileRecord]) -> int:
    """Record discoveries in one short transaction; never touch descriptions."""
    if not records:
        return 0
    stamp = _timestamp()
    rows = [(r.path, r.parent_dir, r.extension, stamp) for r in records]
    with conn:
        conn.executemany(
            """
            INSERT INTO files (path, parent_dir, extension, description, last_seen)
            VALUES (?, ?, ?, '', ?)
            ON CONFLICT(path) DO UPDATE SET
                parent_dir = excluded.parent_dir,
                extension = excluded.extension,
                last_seen = excluded.last_seen
            """,
            rows,
        )
    return len(rows)


def set_description(conn: sqlite3.Connection, path: str, description: str) -> bool:
    """Commit a description immediately so it survives a later crash.

    Returns False if no catalogued file has that path, so a caller never
    silently discards something the user typed.
    """
    with conn:
        cursor = conn.execute(
            "UPDATE files SET description = ? WHERE path = ?", (description, path)
        )
    return cursor.rowcount > 0


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

LIKE_ESCAPE = "\\"


def _like_pattern(query: str) -> str:
    """Build a LIKE pattern matching `query` as a literal substring."""
    escaped = (
        query.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def _entry(row: sqlite3.Row) -> CatalogEntry:
    return CatalogEntry(
        path=row["path"],
        parent_dir=row["parent_dir"],
        extension=row["extension"],
        description=row["description"],
        last_seen=row["last_seen"],
    )


def list_directories(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Directories that contain catalogued files, with their file counts."""
    rows = conn.execute(
        "SELECT parent_dir, COUNT(*) AS n FROM files GROUP BY parent_dir"
        " ORDER BY parent_dir COLLATE NOCASE"
    ).fetchall()
    return [(row["parent_dir"], row["n"]) for row in rows]


def list_files(
    conn: sqlite3.Connection,
    *,
    parent_dir: str | None = None,
    query: str | None = None,
) -> list[CatalogEntry]:
    """Catalogued files, optionally restricted to a directory and a filter.

    The filter is a case-insensitive substring match over path and description.
    """
    sql = ["SELECT * FROM files"]
    clauses: list[str] = []
    params: list[object] = []
    if parent_dir:
        clauses.append("parent_dir = ?")
        params.append(parent_dir)
    if query:
        clauses.append(
            f"(path LIKE ? ESCAPE '{LIKE_ESCAPE}' COLLATE NOCASE"
            f" OR description LIKE ? ESCAPE '{LIKE_ESCAPE}' COLLATE NOCASE)"
        )
        like = _like_pattern(query)
        params.extend([like, like])
    if clauses:
        sql.append("WHERE " + " AND ".join(clauses))
    sql.append("ORDER BY parent_dir COLLATE NOCASE, path COLLATE NOCASE")
    rows = conn.execute(" ".join(sql), params).fetchall()
    return [_entry(row) for row in rows]


def get_file(conn: sqlite3.Connection, path: str) -> CatalogEntry | None:
    row = conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
    return _entry(row) if row else None


def count_files(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])


# --------------------------------------------------------------------------
# Scan driver
# --------------------------------------------------------------------------

def run_scan(
    root: str | os.PathLike[str] = "/",
    *,
    db_path: str | os.PathLike[str] | None = None,
    conn: sqlite3.Connection | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[ScanStats], None] | None = None,
    on_batch: Callable[[Sequence[FileRecord], ScanStats], None] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> ScanStats:
    """Walk `root` and commit discoveries in batches.

    The connection is created here unless one is supplied, so a worker thread
    owns the connection it writes through.
    """
    owned = conn is None
    connection = conn if conn is not None else connect(db_path)
    stats = ScanStats()
    batch: list[FileRecord] = []
    try:
        for record in walk_tree(
            root, stats, should_cancel=should_cancel, on_progress=on_progress
        ):
            batch.append(record)
            if len(batch) >= batch_size:
                upsert_files(connection, batch)
                if on_batch is not None:
                    on_batch(tuple(batch), stats)
                batch.clear()
        if batch:
            upsert_files(connection, batch)
            if on_batch is not None:
                on_batch(tuple(batch), stats)
            batch.clear()
    finally:
        # Partial discoveries already committed above are kept on purpose.
        if batch:
            upsert_files(connection, batch)
        stats.finished = True
        if on_progress is not None:
            on_progress(stats)
        if owned:
            connection.close()
    return stats
