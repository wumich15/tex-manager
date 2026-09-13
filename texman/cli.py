"""Command-line entry point: `texman`, `texman scan`, and `texman ai`.

Argument parsing is kept separate from launching the terminal UI so that
`texman ai` never starts the UI, opens the catalog, or scans the computer.
"""

from __future__ import annotations

import argparse
import signal
import sqlite3
import sys
import threading
import time
from typing import Sequence

from . import __version__

FULL_DISK_ACCESS_NOTE = (
    "Some paths were skipped. On macOS you can grant your terminal Full Disk "
    "Access in System Settings > Privacy & Security, then rescan. Unmounted "
    "drives cannot be scanned."
)


def _catalog_error(db_path: str | None, exc: Exception) -> str:
    """Explain an unusable catalog file instead of showing a traceback."""
    from . import index

    location = db_path or str(index.default_db_path())
    return (
        f"texman: cannot use the catalog at {location}: {exc}\n"
        "If that file is not a texman database, pass a different --db path, or "
        "move the file aside and let texman create a new catalog."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="texman",
        description="Personal catalog of .tex and .sty files, with a Neovim AI helper.",
    )
    parser.add_argument("--version", action="version", version=f"texman {__version__}")
    parser.add_argument(
        "--db",
        metavar="PATH",
        help="SQLite catalog to use (default: $XDG_DATA_HOME/texman/index.sqlite3)",
    )
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="scan for .tex and .sty files without the UI")
    scan.add_argument(
        "--root",
        default="/",
        metavar="DIRECTORY",
        help="directory to scan (default: / , the whole accessible computer)",
    )
    scan.add_argument(
        "--quiet", action="store_true", help="print only the final summary"
    )

    sub.add_parser(
        "ai",
        help="internal: read one JSON request on stdin, write a LaTeX snippet to stdout",
    )
    return parser


def _run_scan(args: argparse.Namespace) -> int:
    import os

    from . import index

    if not os.path.isdir(args.root):
        what = "is not a directory" if os.path.exists(args.root) else "does not exist"
        print(f"texman: scan root {args.root!r} {what}", file=sys.stderr)
        return 2

    last = [0.0]
    stop = threading.Event()

    def request_stop(signum, frame) -> None:
        # The first Ctrl-C stops the walk between directories, so the summary
        # is honest about being incomplete; a second one exits immediately.
        stop.set()
        signal.signal(signal.SIGINT, signal.default_int_handler)
        print("\nStopping scan …", file=sys.stderr)

    signal.signal(signal.SIGINT, request_stop)

    def on_progress(stats: index.ScanStats) -> None:
        now = time.monotonic()
        if args.quiet or now - last[0] < 0.5:
            return
        last[0] = now
        print(
            f"\r{stats.files_found} files | {stats.dirs_visited} dirs | "
            f"{stats.skipped} skipped | {stats.current_dir[:60]}",
            end="",
            file=sys.stderr,
            flush=True,
        )

    try:
        stats = index.run_scan(
            args.root,
            db_path=args.db,
            on_progress=on_progress,
            should_cancel=stop.is_set,
        )
    except KeyboardInterrupt:  # a second Ctrl-C
        print("\nScan interrupted; discoveries already written were kept.", file=sys.stderr)
        return 130
    except sqlite3.Error as exc:
        print(_catalog_error(args.db, exc), file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGINT, signal.default_int_handler)
    if not args.quiet:
        print("\r" + " " * 100, end="\r", file=sys.stderr)
    print(stats.summary())
    if stats.skipped_samples:
        print("Representative skipped paths:")
        for path in stats.skipped_samples:
            print(f"  {path}")
        print(FULL_DISK_ACCESS_NOTE)
    return 0 if stats.complete else 1


def _run_tui(args: argparse.Namespace) -> int:
    from .tui import TexmanApp

    app = TexmanApp(db_path=args.db)
    try:
        app.run()
    except sqlite3.Error as exc:
        print(_catalog_error(args.db, exc), file=sys.stderr)
        return 2
    return 0


def _run_ai(args: argparse.Namespace) -> int:
    from . import ai

    return ai.main()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "ai":
        return _run_ai(args)
    if args.command == "scan":
        return _run_scan(args)
    return _run_tui(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
