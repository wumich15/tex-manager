"""Checks for the preamble template and documents created from it."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from texman import documents


class LocationTests(unittest.TestCase):
    def test_env_override_wins(self) -> None:
        with mock.patch.dict(os.environ, {documents.PREAMBLE_ENV: "~/mine/pre.tex"}):
            path = documents.default_preamble_path()
        self.assertEqual(path, Path(os.path.expanduser("~/mine/pre.tex")))

    def test_default_lives_in_the_config_directory(self) -> None:
        env = {"XDG_CONFIG_HOME": "/tmp/xdg-config"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop(documents.PREAMBLE_ENV, None)
            path = documents.default_preamble_path()
        self.assertEqual(path, Path("/tmp/xdg-config/texman/preamble.tex"))


class TemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.preamble = self.root / "config" / "preamble.tex"

    def test_ensure_preamble_creates_once_and_never_overwrites(self) -> None:
        self.assertTrue(documents.ensure_preamble(self.preamble))
        self.assertIn("\\documentclass", self.preamble.read_text())
        self.preamble.write_text("% mine\n")
        self.assertFalse(documents.ensure_preamble(self.preamble))
        self.assertEqual(self.preamble.read_text(), "% mine\n")

    def test_render_appends_a_body_when_the_template_is_only_a_preamble(self) -> None:
        text = documents.render_document("\\documentclass{article}\n")
        self.assertTrue(text.startswith("\\documentclass{article}\n"))
        self.assertIn("\\begin{document}\n\n\\end{document}\n", text)
        self.assertEqual(text.count("\\begin{document}"), 1)

    def test_render_keeps_a_whole_skeleton_verbatim(self) -> None:
        skeleton = "\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}"
        self.assertEqual(documents.render_document(skeleton), skeleton + "\n")

    def test_names_get_a_tex_extension(self) -> None:
        self.assertEqual(documents.document_name(" notes "), "notes.tex")
        self.assertEqual(documents.document_name("notes.tex"), "notes.tex")
        self.assertEqual(documents.document_name("NOTES.TEX"), "NOTES.TEX")
        self.assertEqual(documents.document_name("v1.2"), "v1.2.tex")

    def test_bad_names_are_refused(self) -> None:
        for bad in ("", "   ", "a/b", "..", "."):
            with self.assertRaises(documents.DocumentError):
                documents.document_name(bad)

    def test_create_uses_the_template_and_reports_it(self) -> None:
        self.preamble.parent.mkdir()
        self.preamble.write_text("\\documentclass{book}\n\\usepackage{foo}\n")
        path, from_template = documents.create_document(
            str(self.root / "papers"), "draft", self.preamble
        )
        self.assertTrue(from_template)
        self.assertEqual(path, str(self.root / "papers" / "draft.tex"))
        text = Path(path).read_text()
        self.assertTrue(text.startswith("\\documentclass{book}\n\\usepackage{foo}\n"))
        self.assertIn("\\begin{document}", text)

    def test_create_falls_back_to_the_default_without_a_template(self) -> None:
        path, from_template = documents.create_document(
            str(self.root), "draft", self.preamble
        )
        self.assertFalse(from_template)
        self.assertIn(documents.DEFAULT_PREAMBLE, Path(path).read_text())
        self.assertFalse(self.preamble.exists())  # never created as a side effect

    def test_create_makes_a_missing_directory_and_expands_tilde(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            path, _ = documents.create_document("~/new/project", "a", self.preamble)
        self.assertEqual(Path(path), self.root / "new" / "project" / "a.tex")
        self.assertTrue(Path(path).is_file())

    def test_create_never_overwrites(self) -> None:
        existing = self.root / "keep.tex"
        existing.write_text("precious\n")
        with self.assertRaises(documents.DocumentError) as caught:
            documents.create_document(str(self.root), "keep", self.preamble)
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(existing.read_text(), "precious\n")

    def test_a_file_where_the_directory_should_be_is_refused(self) -> None:
        blocker = self.root / "blocker"
        blocker.write_text("")
        with self.assertRaises(documents.DocumentError):
            documents.create_document(str(blocker), "x", self.preamble)

    def test_missing_directory_field_is_refused(self) -> None:
        with self.assertRaises(documents.DocumentError):
            documents.create_document("  ", "x", self.preamble)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
