"""Checks for the command-line surface, exercised as a real subprocess.

These confirm the installed entry point works from any directory and that
`texman ai` is wired to the helper without starting the UI or a scan. No API
key is needed: every case here fails before a request would be made.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from texman import index
from tests.test_index import build_fixture

TEXMAN = [sys.executable, "-m", "texman.cli"]


def run(args, *, cwd=None, stdin=None, env=None):
    environment = dict(os.environ)
    environment.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    if env:
        environment.update(env)
    return subprocess.run(
        TEXMAN + args,
        cwd=cwd or tempfile.gettempdir(),
        input=stdin,
        capture_output=True,
        text=True,
        env=environment,
    )


class CommandTests(unittest.TestCase):
    def test_help_works_from_another_directory(self) -> None:
        result = run(["--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("texman", result.stdout)
        self.assertIn("scan", result.stdout)

    def test_version_is_reported(self) -> None:
        result = run(["--version"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("texman", result.stdout)


class ScanCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.files = build_fixture(self.root)
        self.db = self.root / "index.sqlite3"
        self.addCleanup(self.tmp.cleanup)

    def test_scan_root_populates_the_given_database(self) -> None:
        result = run(["--db", str(self.db), "scan", "--root", str(self.root), "--quiet"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("complete", result.stdout)
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(index.count_files(conn), len(self.files))

    def test_scan_reports_skipped_paths(self) -> None:
        locked = self.root / "locked"
        locked.mkdir()
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o700)
        if os.geteuid() == 0:  # pragma: no cover - root ignores permissions
            self.skipTest("running as root defeats the permission check")
        result = run(["--db", str(self.db), "scan", "--root", str(self.root), "--quiet"])
        self.assertIn("skipped", result.stdout)
        self.assertIn("Full Disk Access", result.stdout)


class ScanRootValidationTests(unittest.TestCase):
    def test_missing_root_is_an_error_not_a_complete_scan(self) -> None:
        result = run(["scan", "--root", "/definitely-not-here", "--quiet"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not exist", result.stderr)
        self.assertNotIn("complete", result.stdout)

    def test_file_as_root_is_an_error(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".tex") as handle:
            result = run(["scan", "--root", handle.name, "--quiet"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("is not a directory", result.stderr)

    def test_unusable_catalog_file_is_explained(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            bogus = Path(folder) / "not-a-catalog.sqlite3"
            bogus.write_text("this is plain text, not a database")
            result = run(["--db", str(bogus), "scan", "--root", folder, "--quiet"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot use the catalog", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class AiCommandTests(unittest.TestCase):
    """`texman ai` must never start the UI or scan, and must fail cleanly."""

    def test_invalid_json_exits_nonzero_without_stdout(self) -> None:
        result = run(["ai"], stdin="not json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("texman ai:", result.stderr)

    def test_invalid_line_is_rejected_before_any_request(self) -> None:
        payload = json.dumps({"line": 99, "prompt": "p", "buffer_lines": ["a"]})
        result = run(["ai"], stdin=payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("out of range", result.stderr)

    def test_missing_model_is_reported(self) -> None:
        payload = json.dumps({"line": 1, "prompt": "p", "buffer_lines": ["a"]})
        result = run(["ai"], stdin=payload, env={"TEXMAN_OPENAI_MODEL": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("TEXMAN_OPENAI_MODEL", result.stderr)

    def test_missing_key_is_reported(self) -> None:
        payload = json.dumps({"line": 1, "prompt": "p", "buffer_lines": ["a"]})
        env = {"TEXMAN_OPENAI_MODEL": "some-model", "OPENAI_API_KEY": ""}
        result = run(["ai"], stdin=payload, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("OPENAI_API_KEY", result.stderr)

    def test_interactive_use_is_refused_instead_of_hanging(self) -> None:
        """With a terminal on stdin there is no request to read, so say so."""
        import io
        from texman import ai

        class Tty(io.StringIO):
            def isatty(self) -> bool:
                return True

        err = io.StringIO()
        code = ai.main(stdin=Tty(), stdout=io.StringIO(), stderr=err)
        self.assertNotEqual(code, 0)
        self.assertIn("standard input", err.getvalue())

    def test_ai_does_not_touch_the_catalog(self) -> None:
        db = Path(tempfile.mkdtemp()) / "never-created.sqlite3"
        payload = json.dumps({"line": 5, "prompt": "p", "buffer_lines": ["a"]})
        result = run(["--db", str(db), "ai"], stdin=payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(db.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
