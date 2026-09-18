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
import unittest.mock
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

    def test_preamble_option_is_offered(self) -> None:
        result = run(["--help"])
        self.assertIn("--preamble", result.stdout)

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


class DefaultRootTests(unittest.TestCase):
    """`texman` scans ~/Documents and ~/Downloads unless told otherwise."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.db = self.home / "index.sqlite3"
        self.addCleanup(self.tmp.cleanup)

    def _make(self, *names: str) -> None:
        for name in names:
            (self.home / name).mkdir()
            (self.home / name / f"{name.lower()}.tex").write_text("x")

    def test_scan_without_root_uses_documents_and_downloads(self) -> None:
        self._make("Documents", "Downloads")
        (self.home / "Elsewhere").mkdir()
        (self.home / "Elsewhere" / "ignored.tex").write_text("x")
        result = run(
            ["--db", str(self.db), "scan", "--quiet"], env={"HOME": str(self.home)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("~/Documents, ~/Downloads", result.stdout)
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        paths = {entry.path for entry in index.list_files(conn)}
        self.assertEqual(
            paths,
            {
                str(self.home / "Documents" / "documents.tex"),
                str(self.home / "Downloads" / "downloads.tex"),
            },
        )

    def test_several_roots_can_be_given(self) -> None:
        self._make("Documents", "Downloads", "Elsewhere")
        result = run(
            [
                "--db", str(self.db), "scan",
                "--root", str(self.home / "Documents"),
                "--root", str(self.home / "Elsewhere"),
                "--quiet",
            ],
            env={"HOME": str(self.home)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        paths = {entry.path for entry in index.list_files(conn)}
        self.assertEqual(
            paths,
            {
                str(self.home / "Documents" / "documents.tex"),
                str(self.home / "Elsewhere" / "elsewhere.tex"),
            },
        )

    def test_root_given_before_the_subcommand_is_not_discarded(self) -> None:
        self._make("Documents", "Elsewhere")
        result = run(
            [
                "--db", str(self.db),
                "--root", str(self.home / "Elsewhere"),
                "scan", "--quiet",
            ],
            env={"HOME": str(self.home)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        conn = index.connect(self.db)
        self.addCleanup(conn.close)
        paths = {entry.path for entry in index.list_files(conn)}
        self.assertEqual(paths, {str(self.home / "Elsewhere" / "elsewhere.tex")})

    def test_absent_default_roots_are_explained(self) -> None:
        result = run(
            ["--db", str(self.db), "scan", "--quiet"], env={"HOME": str(self.home)}
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("~/Documents", result.stderr)
        self.assertIn("--root", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


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

    def test_keymap_requests_need_no_buffer(self) -> None:
        payload = json.dumps({"mode": "keymap", "prompt": "an enumerate shortcut"})
        env = {"TEXMAN_OPENAI_MODEL": "some-model", "OPENAI_API_KEY": ""}
        result = run(["ai"], stdin=payload, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        # Validation passed; the request failed only for lack of a key.
        self.assertIn("OPENAI_API_KEY", result.stderr)

    def test_ai_does_not_touch_the_catalog(self) -> None:
        db = Path(tempfile.mkdtemp()) / "never-created.sqlite3"
        payload = json.dumps({"line": 5, "prompt": "p", "buffer_lines": ["a"]})
        result = run(["--db", str(db), "ai"], stdin=payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(db.exists())

    def test_a_nonsense_window_is_reported_not_raised(self) -> None:
        payload = json.dumps({"line": 1, "prompt": "p", "buffer_lines": ["a"]})
        result = run(["ai"], stdin=payload, env={"TEXMAN_WINDOW_LINES": "plenty"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("TEXMAN_WINDOW_LINES", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class PreambleCommandTests(unittest.TestCase):
    """`texman preamble` summarises the template, and explains any refusal."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.preamble = Path(self.tmp.name) / "preamble.tex"
        self.preamble.write_text("\\documentclass{article}\n", encoding="utf-8")
        # Never the developer's own cache.
        self.env = {
            "XDG_DATA_HOME": str(Path(self.tmp.name) / "data"),
            "TEXMAN_PREAMBLE": str(self.preamble),
        }

    def test_it_is_offered_in_the_help(self) -> None:
        self.assertIn("preamble", run(["--help"]).stdout)

    def test_missing_key_is_reported(self) -> None:
        env = {
            **self.env,
            "TEXMAN_OPENAI_MINI_MODEL": "mini",
            "OPENAI_API_KEY": "",
        }
        result = run(["preamble"], env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OPENAI_API_KEY", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_mini_model_is_reported(self) -> None:
        env = {
            **self.env,
            "OPENAI_API_KEY": "sk-not-used",
            "TEXMAN_OPENAI_MINI_MODEL": "",
        }
        result = run(["preamble"], env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TEXMAN_OPENAI_MINI_MODEL", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_a_missing_preamble_is_reported(self) -> None:
        self.preamble.unlink()
        env = {
            **self.env,
            "OPENAI_API_KEY": "sk-not-used",
            "TEXMAN_OPENAI_MINI_MODEL": "mini",
        }
        result = run(["preamble"], env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no preamble", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_a_cached_summary_needs_no_request(self) -> None:
        """A warm cache is reported without a key or a model being configured."""
        from texman import preamble

        data = Path(self.tmp.name) / "data"
        with unittest.mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": str(data)}, clear=False
        ):
            preamble.store(
                self.preamble,
                self.preamble.read_text(encoding="utf-8"),
                "article class",
                "mini",
            )
        env = {**self.env, "OPENAI_API_KEY": "", "TEXMAN_OPENAI_MINI_MODEL": ""}
        result = run(["preamble", "--show"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already current", result.stdout)
        self.assertIn("article class", result.stdout)

    def test_it_does_not_touch_the_catalog(self) -> None:
        db = Path(self.tmp.name) / "never-created.sqlite3"
        env = {**self.env, "TEXMAN_OPENAI_MINI_MODEL": "mini", "OPENAI_API_KEY": ""}
        result = run(["--db", str(db), "preamble"], env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(db.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
