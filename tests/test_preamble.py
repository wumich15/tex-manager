"""Checks for the cached preamble digest.

Nothing here talks to OpenAI: `ensure_digest` takes the function that produces a
summary, so these tests hand it one that counts its own calls. Every test points
XDG_DATA_HOME at a temporary directory, so the developer's own cache is never
read or written.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from texman import preamble

SAMPLE = "\\documentclass{article}\n\\usepackage{amsmath}\n"
SUMMARY = "article, 11pt\namsmath loaded\n"


class DigestCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = Path(self.tmp.name) / "preamble.tex"
        self.path.write_text(SAMPLE, encoding="utf-8")
        self.calls: list[str] = []

    def produce(self, text: str) -> tuple[str, str]:
        self.calls.append(text)
        return SUMMARY, "mini-model"

    # ------------------------------------------------------------ location

    def test_cache_sits_beside_the_catalog(self) -> None:
        self.assertEqual(
            preamble.cache_path(),
            Path(self.tmp.name) / "texman" / preamble.CACHE_NAME,
        )

    # ------------------------------------------------------- round tripping

    def test_stores_and_reloads_the_digest(self) -> None:
        record = preamble.store(self.path, SAMPLE, SUMMARY, "mini-model")
        self.assertEqual(record.model, "mini-model")
        again = preamble.load(self.path)
        self.assertIsNotNone(again)
        self.assertEqual(again.text, SUMMARY.strip())
        self.assertEqual(again.path, os.path.realpath(self.path))

    def test_generates_once_and_then_reuses(self) -> None:
        first, generated = preamble.ensure_digest(self.path, self.produce)
        self.assertTrue(generated)
        second, generated_again = preamble.ensure_digest(self.path, self.produce)
        self.assertFalse(generated_again)
        self.assertEqual(second.text, first.text)
        self.assertEqual(len(self.calls), 1)

    def test_force_summarises_again(self) -> None:
        preamble.ensure_digest(self.path, self.produce)
        _, generated = preamble.ensure_digest(self.path, self.produce, force=True)
        self.assertTrue(generated)
        self.assertEqual(len(self.calls), 2)

    # ---------------------------------------------------------- staleness

    def test_an_edited_preamble_invalidates_the_cache(self) -> None:
        preamble.ensure_digest(self.path, self.produce)
        self.path.write_text(SAMPLE + "\\usepackage{tikz}\n", encoding="utf-8")
        _, generated = preamble.ensure_digest(self.path, self.produce)
        self.assertTrue(generated)
        self.assertEqual(len(self.calls), 2)

    def test_a_different_preamble_is_a_miss(self) -> None:
        preamble.store(self.path, SAMPLE, SUMMARY, "mini-model")
        other = Path(self.tmp.name) / "other.tex"
        other.write_text(SAMPLE, encoding="utf-8")
        self.assertIsNone(preamble.load(other))

    def test_a_corrupt_cache_is_a_miss_not_an_error(self) -> None:
        preamble.store(self.path, SAMPLE, SUMMARY, "mini-model")
        preamble.cache_path().write_text("{ not json", encoding="utf-8")
        self.assertIsNone(preamble.load(self.path))
        _, generated = preamble.ensure_digest(self.path, self.produce)
        self.assertTrue(generated)

    def test_a_cache_that_is_not_an_object_is_a_miss(self) -> None:
        preamble.cache_path().parent.mkdir(parents=True, exist_ok=True)
        preamble.cache_path().write_text("[1, 2, 3]", encoding="utf-8")
        self.assertIsNone(preamble.load(self.path))

    def test_an_empty_cached_digest_is_a_miss(self) -> None:
        preamble.store(self.path, SAMPLE, SUMMARY, "mini-model")
        record = json.loads(preamble.cache_path().read_text(encoding="utf-8"))
        record["digest"] = "   "
        preamble.cache_path().write_text(json.dumps(record), encoding="utf-8")
        self.assertIsNone(preamble.load(self.path))

    def test_a_missing_cache_is_a_miss(self) -> None:
        self.assertIsNone(preamble.load(self.path))

    # ------------------------------------------------------------- errors

    def test_a_missing_preamble_is_a_clean_error(self) -> None:
        with self.assertRaises(preamble.DigestError) as caught:
            preamble.ensure_digest(Path(self.tmp.name) / "absent.tex", self.produce)
        self.assertIn("no preamble", str(caught.exception))

    def test_an_empty_summary_is_refused(self) -> None:
        with self.assertRaises(preamble.DigestError):
            preamble.ensure_digest(self.path, lambda text: ("  \n ", "mini-model"))
        self.assertFalse(preamble.cache_path().exists())

    # -------------------------------------------------------------- bounds

    def test_a_long_preamble_is_clipped_before_it_is_sent(self) -> None:
        long = "x" * (preamble.MAX_PREAMBLE_CHARS + 5_000)
        self.path.write_text(long, encoding="utf-8")
        preamble.ensure_digest(self.path, self.produce)
        sent = self.calls[0]
        self.assertLess(len(sent), preamble.MAX_PREAMBLE_CHARS + 100)
        self.assertIn("omitted", sent)

    def test_a_short_preamble_is_sent_whole(self) -> None:
        preamble.ensure_digest(self.path, self.produce)
        self.assertEqual(self.calls[0], SAMPLE)

    # -------------------------------------------------------------- writes

    def test_a_failed_write_leaves_no_temporary_file(self) -> None:
        with mock.patch("json.dump", side_effect=OSError("disk full")):
            with self.assertRaises(preamble.DigestError):
                preamble.store(self.path, SAMPLE, SUMMARY, "mini-model")
        leftovers = list(preamble.cache_path().parent.glob(preamble.CACHE_NAME + "*"))
        self.assertEqual(leftovers, [])

    def test_a_rewrite_replaces_the_previous_digest(self) -> None:
        preamble.store(self.path, SAMPLE, "first", "mini-model")
        preamble.store(self.path, SAMPLE, "second", "mini-model")
        self.assertEqual(preamble.load(self.path).text, "second")
        self.assertEqual(
            len(list(preamble.cache_path().parent.glob(preamble.CACHE_NAME + "*"))), 1
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
