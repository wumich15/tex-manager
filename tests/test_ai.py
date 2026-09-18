"""Checks for the `texman ai` contract.

The OpenAI client is always a stub, so these tests make no paid API calls and
need neither a key nor a network connection. The whole module also runs with
XDG pointed at a temporary directory, because a generation request now looks up
a cached preamble summary and would otherwise read and write the developer's
own cache.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from texman import ai, documents, preamble

_isolation: tempfile.TemporaryDirectory | None = None
_environment: object | None = None


def setUpModule() -> None:
    """Keep every check in this file away from the developer's own files."""
    global _isolation, _environment
    _isolation = tempfile.TemporaryDirectory()
    _environment = mock.patch.dict(
        os.environ,
        {
            "XDG_DATA_HOME": os.path.join(_isolation.name, "data"),
            "XDG_CONFIG_HOME": os.path.join(_isolation.name, "config"),
        },
    )
    _environment.start()
    for name in (documents.PREAMBLE_ENV, ai.MINI_MODEL_ENV, ai.WINDOW_ENV):
        os.environ.pop(name, None)


def tearDownModule() -> None:
    _environment.stop()
    _isolation.cleanup()


def response(
    text: str = "\\begin{equation}\n  x = 1\n\\end{equation}",
    *,
    status: str = "completed",
    refusal: str | None = None,
    incomplete_reason: str | None = None,
) -> SimpleNamespace:
    output = []
    if refusal is not None:
        output = [
            SimpleNamespace(
                content=[SimpleNamespace(type="refusal", refusal=refusal)]
            )
        ]
    return SimpleNamespace(
        output_text=text,
        status="incomplete" if incomplete_reason else status,
        incomplete_details=(
            SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None
        ),
        output=output,
    )


class StubClient:
    """Records the single request the helper is allowed to make."""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result if result is not None else response()
        self.error = error
        self.calls: list[dict] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def status_error(cls, status: int, message: str = "failed"):
    """Build an SDK status error without depending on its HTTP library."""
    response = mock.Mock(status_code=status, headers={})
    return cls(message, response=response, body=None)


def request(line: int = 1, prompt: str = "add a matrix", lines=("a", "b")) -> ai.AiRequest:
    return ai.AiRequest(line=line, prompt=prompt, buffer_lines=list(lines))


class ParseRequestTests(unittest.TestCase):
    def payload(self, **overrides) -> str:
        body = {"line": 2, "prompt": "add a matrix", "buffer_lines": ["a", "b", "c"]}
        body.update(overrides)
        return json.dumps(body)

    def test_accepts_a_well_formed_request(self) -> None:
        parsed = ai.parse_request(self.payload())
        self.assertEqual(parsed.line, 2)
        self.assertEqual(parsed.prompt, "add a matrix")
        self.assertEqual(parsed.buffer_lines, ["a", "b", "c"])

    def test_accepts_one_past_the_last_line(self) -> None:
        self.assertEqual(ai.parse_request(self.payload(line=4)).line, 4)

    def test_rejects_line_beyond_append_position(self) -> None:
        with self.assertRaisesRegex(ai.AiError, "out of range"):
            ai.parse_request(self.payload(line=5))

    def test_rejects_zero_and_negative_lines(self) -> None:
        for bad in (0, -1):
            with self.subTest(line=bad), self.assertRaises(ai.AiError):
                ai.parse_request(self.payload(line=bad))

    def test_rejects_non_integer_lines(self) -> None:
        for bad in ("2", 2.5, True, None):
            with self.subTest(line=bad), self.assertRaises(ai.AiError):
                ai.parse_request(self.payload(line=bad))

    def test_rejects_empty_or_missing_prompt(self) -> None:
        for bad in ("", "   ", None, 7):
            with self.subTest(prompt=bad), self.assertRaises(ai.AiError):
                ai.parse_request(self.payload(prompt=bad))

    def test_rejects_bad_buffer_lines(self) -> None:
        for bad in ("abc", [1, 2], None, {"a": 1}):
            with self.subTest(buffer=bad), self.assertRaises(ai.AiError):
                ai.parse_request(self.payload(buffer_lines=bad))

    def test_rejects_invalid_json_and_non_objects(self) -> None:
        for raw in ("", "not json", "[1, 2]", '"text"'):
            with self.subTest(raw=raw), self.assertRaises(ai.AiError):
                ai.parse_request(raw)

    def test_prompt_is_trimmed_but_kept_verbatim_inside(self) -> None:
        prompt = r"  draw \tikz{a} and $x \le y$  "
        self.assertEqual(
            ai.parse_request(self.payload(prompt=prompt)).prompt,
            r"draw \tikz{a} and $x \le y$",
        )


# A log in the shape latexmk -file-line-error actually produces.
REAL_LOG = """\
This is pdfTeX, Version 3.141592653-2.6-1.40.28
entering extended mode
LaTeX Font Info:    Checking defaults for OML/cmm/m/it on input line 4.
LaTeX Font Info:    ... okay on input line 4.
LaTeX Font Info:    Checking defaults for U/cmr/m/n on input line 4.

./broken.tex:7: Undefined control sequence.
l.7 Some text with a bad macro: \\undefinedmacro
                                                and more text.
The control sequence at the end of the top line
of your error message was never \\def'ed.

./broken.tex:13: Missing } inserted.
<inserted text>
                }
l.13 Text with a lone brace }
Overfull \\hbox (12.0pt too wide) in paragraph at lines 20--21
LaTeX Warning: Reference `sec:one' on page 1 undefined on input line 25.
Output written on broken.pdf (1 page, 12345 bytes).
"""


class FixModeValidationTests(unittest.TestCase):
    def payload(self, **overrides) -> str:
        body = {
            "mode": "fix",
            "line": 2,
            "prompt": "Undefined control sequence.",
            "buffer_lines": ["a", "b", "c"],
            "log_text": REAL_LOG,
        }
        body.update(overrides)
        return json.dumps(body)

    def test_accepts_a_fix_request(self) -> None:
        parsed = ai.parse_request(self.payload())
        self.assertEqual(parsed.mode, ai.MODE_FIX)
        self.assertEqual(parsed.line, 2)
        self.assertIn("Undefined control sequence", parsed.log_text)

    def test_mode_defaults_to_insert(self) -> None:
        body = json.dumps({"line": 1, "prompt": "p", "buffer_lines": ["a"]})
        self.assertEqual(ai.parse_request(body).mode, ai.MODE_INSERT)

    def test_unknown_mode_is_rejected(self) -> None:
        for bad in ("replace", "", None, 3):
            with self.subTest(mode=bad), self.assertRaisesRegex(ai.AiError, "mode"):
                ai.parse_request(self.payload(mode=bad))

    def test_fix_cannot_target_the_append_position(self) -> None:
        """Inserting before line N + 1 appends; replacing it is meaningless."""
        with self.assertRaisesRegex(ai.AiError, "valid lines are 1 to 3"):
            ai.parse_request(self.payload(line=4))

    def test_fix_requires_compiler_output(self) -> None:
        for bad in ("", "   ", None):
            with self.subTest(log=bad), self.assertRaisesRegex(ai.AiError, "log_text"):
                ai.parse_request(self.payload(log_text=bad))

    def test_log_text_must_be_a_string(self) -> None:
        with self.assertRaisesRegex(ai.AiError, "log_text must be a string"):
            ai.parse_request(self.payload(log_text=["a line"]))

    def test_insert_mode_ignores_a_missing_log(self) -> None:
        body = json.dumps({"line": 1, "prompt": "p", "buffer_lines": ["a"]})
        self.assertEqual(ai.parse_request(body).log_text, "")


class LogExcerptTests(unittest.TestCase):
    def test_keeps_errors_and_drops_font_chatter(self) -> None:
        excerpt = ai.extract_log_excerpt(REAL_LOG)
        self.assertIn("./broken.tex:7: Undefined control sequence.", excerpt)
        self.assertIn("l.7 Some text with a bad macro:", excerpt)
        self.assertIn("./broken.tex:13: Missing } inserted.", excerpt)
        self.assertNotIn("entering extended mode", excerpt)
        self.assertNotIn("Checking defaults for OML", excerpt)

    def test_labels_omitted_stretches(self) -> None:
        log = "\n".join(
            ["./doc.tex:1: First error."]
            + [f"LaTeX Font Info:    chatter {n}" for n in range(40)]
            + ["./doc.tex:90: Second error."]
        )
        excerpt = ai.extract_log_excerpt(log)
        self.assertIn("./doc.tex:1: First error.", excerpt)
        self.assertIn("./doc.tex:90: Second error.", excerpt)
        self.assertIn("log line(s) omitted ...]", excerpt)
        self.assertNotIn("chatter 20", excerpt)

    def test_includes_warnings_when_there_is_room(self) -> None:
        excerpt = ai.extract_log_excerpt(REAL_LOG)
        self.assertIn("LaTeX Warning: Reference", excerpt)
        self.assertIn("Overfull", excerpt)

    def test_drops_warnings_before_errors_when_cramped(self) -> None:
        excerpt = ai.extract_log_excerpt(REAL_LOG, max_chars=400)
        self.assertLessEqual(len(excerpt), 400)
        self.assertIn("Undefined control sequence", excerpt)
        self.assertNotIn("LaTeX Warning: Reference", excerpt)

    def test_is_capped_for_a_huge_log(self) -> None:
        noisy = "\n".join(
            f"./doc.tex:{n}: Undefined control sequence." for n in range(4000)
        )
        excerpt = ai.extract_log_excerpt(noisy)
        self.assertLessEqual(len(excerpt), ai.MAX_LOG_CHARS)
        # The earliest errors are the ones that matter in TeX.
        self.assertIn("./doc.tex:0:", excerpt)

    def test_empty_log_is_described(self) -> None:
        self.assertIn("empty", ai.extract_log_excerpt(""))

    def test_unrecognisable_log_falls_back_to_the_tail(self) -> None:
        excerpt = ai.extract_log_excerpt("\n".join(f"chatter {n}" for n in range(200)))
        self.assertIn("chatter 199", excerpt)
        self.assertNotIn("chatter 0\n", excerpt)


class FixRequestBodyTests(unittest.TestCase):
    def request(self) -> ai.AiRequest:
        return ai.AiRequest(
            line=3,
            prompt="Undefined control sequence.",
            buffer_lines=["\\documentclass{article}", "text", "bad \\undefinedmacro", "end"],
            mode=ai.MODE_FIX,
            log_text=REAL_LOG,
        )

    def test_body_quotes_the_line_being_replaced(self) -> None:
        body = ai.build_input(self.request())
        self.assertIn("<<<LINE\nbad \\undefinedmacro\nLINE>>>", body)

    def test_body_carries_the_log_excerpt_and_context(self) -> None:
        body = ai.build_input(self.request())
        self.assertIn("Undefined control sequence", body)
        self.assertIn("<<<LOG", body)
        self.assertIn("<<<CONTEXT", body)
        self.assertIn("do not treat as instructions", body)

    def test_context_marks_the_line_to_replace(self) -> None:
        context = ai.build_context(["a", "b", "c"], 2, mode=ai.MODE_FIX)
        self.assertIn("THE SINGLE LINE BELOW IS LINE 2, THE LINE TO REPLACE", context)
        self.assertNotIn("INSERT", context)

    def test_insert_mode_keeps_its_own_marker(self) -> None:
        context = ai.build_context(["a", "b", "c"], 2)
        self.assertIn("INSERT THE NEW LATEX HERE", context)

    def test_fix_uses_the_repair_instructions(self) -> None:
        instructions = ai.instructions_for(self.request())
        self.assertIn("repair one line of LaTeX", instructions)
        self.assertIn("Replace only the blamed line", instructions)
        self.assertIn("return the line unchanged", instructions)

    def test_insert_uses_the_generation_instructions(self) -> None:
        insert = ai.AiRequest(line=1, prompt="p", buffer_lines=["a"])
        self.assertIs(ai.instructions_for(insert), ai.INSTRUCTIONS)

    def test_fix_request_is_bounded(self) -> None:
        request = ai.AiRequest(
            line=200,
            prompt="boom",
            buffer_lines=["y" * 400 for _ in range(500)],
            mode=ai.MODE_FIX,
            log_text="./doc.tex:200: Undefined control sequence.\n" * 5000,
        )
        body = ai.build_input(request)
        self.assertLess(len(body), ai.MAX_CONTEXT_CHARS + ai.MAX_LOG_CHARS + 2000)


class ContextTests(unittest.TestCase):
    def test_includes_preamble_and_both_windows(self) -> None:
        buffer = [f"line{i}" for i in range(1, 301)]
        context = ai.build_context(buffer, 150)
        self.assertIn("line1\n", context)
        self.assertIn("line100\n", context)
        # WINDOW_LINES each side of the insertion point.
        self.assertIn("line140\n", context)
        self.assertIn("line159\n", context)
        self.assertNotIn("line139\n", context)
        self.assertNotIn("line160\n", context)

    def test_marks_the_insertion_point(self) -> None:
        context = ai.build_context(["a", "b", "c"], 2)
        self.assertIn("INSERT THE NEW LATEX HERE, before line 2", context)
        self.assertLess(context.index("a"), context.index("INSERT"))
        self.assertGreater(context.index("b"), context.index("INSERT"))

    def test_labels_omitted_sections(self) -> None:
        buffer = [f"line{i}" for i in range(1, 301)]
        context = ai.build_context(buffer, 150)
        self.assertIn("[... lines 101-139 omitted ...]", context)
        self.assertIn("[... lines 160-300 omitted ...]", context)

    def test_append_position_is_described(self) -> None:
        context = ai.build_context(["a"], 2)
        self.assertIn("at the end of the document (line 2)", context)

    def test_empty_buffer_is_handled(self) -> None:
        self.assertIn("INSERT", ai.build_context([], 1))

    def test_overlapping_ranges_are_not_duplicated(self) -> None:
        buffer = [f"line{i}" for i in range(1, 51)]
        context = ai.build_context(buffer, 25)
        self.assertEqual(context.count("line10\n"), 1)

    def test_context_is_capped_and_keeps_nearby_lines(self) -> None:
        buffer = ["x" * 500 for _ in range(400)]
        context = ai.build_context(buffer, 200)
        self.assertLessEqual(len(context), ai.MAX_CONTEXT_CHARS)
        self.assertIn("INSERT THE NEW LATEX HERE, before line 200", context)
        # The lines on either side of the insertion point survive the trimming.
        self.assertIn("   199| ", context)
        self.assertIn("   200| ", context)
        self.assertIn(" omitted ...]", context)

    def test_a_wide_window_sheds_the_preamble_first(self) -> None:
        buffer = ["x" * 500 for _ in range(400)]
        context = ai.build_context(buffer, 200, window=40)
        self.assertLessEqual(len(context), ai.MAX_CONTEXT_CHARS)
        self.assertIn("[... lines 1-", context)
        self.assertIn("INSERT THE NEW LATEX HERE, before line 200", context)

    # ------------------------------------------------------- line numbers

    def test_every_context_line_carries_its_number(self) -> None:
        buffer = [f"line{i}" for i in range(1, 12)]
        context = ai.build_context(buffer, 6)
        self.assertIn("     1| line1", context)
        self.assertIn("    11| line11", context)
        # The number must be the real one, not an offset into the excerpt.
        buffer = [f"line{i}" for i in range(1, 301)]
        context = ai.build_context(buffer, 150)
        self.assertIn("   149| line149", context)
        self.assertIn("   150| line150", context)

    # --------------------------------------------------------- whole file

    def test_full_sends_every_line_with_no_gaps(self) -> None:
        buffer = [f"line{i}" for i in range(1, 501)]
        context = ai.build_context(buffer, 400, full=True)
        self.assertNotIn("omitted", context)
        self.assertIn("     1| line1\n", context)
        self.assertIn("   250| line250\n", context)
        self.assertIn("   500| line500", context)
        self.assertIn("[INSERT THE NEW LATEX HERE, before line 400]", context)

    def test_full_marks_an_append_at_the_end(self) -> None:
        buffer = [f"line{i}" for i in range(1, 6)]
        context = ai.build_context(buffer, 6, full=True)
        self.assertIn("     5| line5", context)
        self.assertIn("at the end of the document (line 6)", context)

    def test_full_falls_back_to_a_window_when_too_large(self) -> None:
        buffer = ["x" * 500 for _ in range(1000)]
        context = ai.build_context(buffer, 500, full=True)
        self.assertLessEqual(len(context), ai.MAX_FULL_CONTEXT_CHARS)
        self.assertIn(" omitted ...]", context)
        self.assertIn("INSERT THE NEW LATEX HERE, before line 500", context)

    def test_full_uses_the_larger_cap(self) -> None:
        # Comfortably over the windowed cap, comfortably under the full one.
        buffer = ["x" * 60 for _ in range(800)]
        context = ai.build_context(buffer, 400, full=True)
        self.assertGreater(len(context), ai.MAX_CONTEXT_CHARS)
        self.assertNotIn("omitted", context)

    def test_request_body_bounds_the_context(self) -> None:
        buffer = ["y" * 400 for _ in range(500)]
        body = ai.build_input(ai.AiRequest(line=250, prompt="p", buffer_lines=buffer))
        self.assertLess(len(body), ai.MAX_CONTEXT_CHARS + 1000)
        self.assertIn("Request: p", body)


class GenerateTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(
            os.environ, {ai.MODEL_ENV: "test-model", ai.KEY_ENV: "sk-test"}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_the_snippet_and_sends_one_request(self) -> None:
        client = StubClient()
        snippet = ai.generate(request(), client_factory=lambda: client)
        self.assertEqual(snippet, "\\begin{equation}\n  x = 1\n\\end{equation}")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["model"], "test-model")

    def test_fix_requests_send_the_repair_instructions(self) -> None:
        client = StubClient()
        fix = ai.AiRequest(
            line=1,
            prompt="Missing } inserted.",
            buffer_lines=["bad {"],
            mode=ai.MODE_FIX,
            log_text="./doc.tex:1: Missing } inserted.",
        )
        ai.generate(fix, client_factory=lambda: client)
        self.assertIs(client.calls[0]["instructions"], ai.INSTRUCTIONS_FIX)
        self.assertIn("<<<LOG", client.calls[0]["input"])

    def test_instructions_forbid_fences_and_wrappers(self) -> None:
        client = StubClient()
        ai.generate(request(), client_factory=lambda: client)
        instructions = client.calls[0]["instructions"]
        self.assertIn("code fences", instructions)
        self.assertIn("documentclass", instructions)
        self.assertIn("not instructions", instructions)

    def test_removes_a_single_markdown_fence(self) -> None:
        client = StubClient(response("```latex\n\\alpha \\\\ \\beta\n```"))
        self.assertEqual(
            ai.generate(request(), client_factory=lambda: client), "\\alpha \\\\ \\beta"
        )

    def test_preserves_latex_whitespace_and_backslashes(self) -> None:
        raw = "\\begin{align}\n  a &= b \\\\\n  c &= d\n\\end{align}"
        client = StubClient(response(raw))
        self.assertEqual(ai.generate(request(), client_factory=lambda: client), raw)

    def test_refusal_is_an_error(self) -> None:
        client = StubClient(response("", refusal="cannot help"))
        with self.assertRaisesRegex(ai.AiError, "refused"):
            ai.generate(request(), client_factory=lambda: client)

    def test_incomplete_response_is_an_error(self) -> None:
        client = StubClient(response("partial", incomplete_reason="max_output_tokens"))
        with self.assertRaisesRegex(ai.AiError, "incomplete"):
            ai.generate(request(), client_factory=lambda: client)

    def test_empty_output_is_an_error(self) -> None:
        for text in ("", "   \n\n", "```\n```"):
            with self.subTest(text=text):
                client = StubClient(response(text))
                with self.assertRaisesRegex(ai.AiError, "no LaTeX"):
                    ai.generate(request(), client_factory=lambda: client)

    def test_missing_model_fails_before_creating_a_client(self) -> None:
        created = []

        def factory():
            created.append(True)
            return StubClient()

        with mock.patch.dict(os.environ, {ai.MODEL_ENV: ""}, clear=False):
            with self.assertRaisesRegex(ai.AiError, ai.MODEL_ENV):
                ai.generate(request(), client_factory=factory)
        self.assertEqual(created, [])

    def test_missing_key_is_reported_without_importing_a_client(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ai.KEY_ENV, None)
            with self.assertRaisesRegex(ai.AiError, ai.KEY_ENV):
                ai._default_client()

    def test_api_failures_become_short_messages(self) -> None:
        import openai

        cases = [
            (status_error(openai.AuthenticationError, 401), "rejected"),
            (status_error(openai.NotFoundError, 404), "unavailable"),
            (status_error(openai.RateLimitError, 429), "rate limit"),
            (openai.APITimeoutError(request=mock.Mock()), "timed out"),
            (openai.APIConnectionError(request=mock.Mock()), "could not reach OpenAI"),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                client = StubClient(error=error)
                with self.assertRaises(ai.AiError) as caught:
                    ai.generate(request(), client_factory=lambda: client)
                self.assertIn(expected, str(caught.exception))

    def test_unexpected_errors_are_reported_without_details(self) -> None:
        client = StubClient(error=RuntimeError("sk-test leaked here"))
        with self.assertRaises(ai.AiError) as caught:
            ai.generate(request(), client_factory=lambda: client)
        self.assertIn("RuntimeError", str(caught.exception))
        self.assertNotIn("sk-test", str(caught.exception))

    def test_errors_never_leak_the_key_or_request_body(self) -> None:
        import openai

        client = StubClient(
            error=status_error(
                openai.AuthenticationError, 401, "Incorrect API key provided: sk-test"
            )
        )
        with self.assertRaises(ai.AiError) as caught:
            ai.generate(request(prompt="secret prompt"), client_factory=lambda: client)
        message = str(caught.exception)
        self.assertNotIn("sk-test", message)
        self.assertNotIn("secret prompt", message)


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(
            os.environ, {ai.MODEL_ENV: "test-model", ai.KEY_ENV: "sk-test"}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, payload, client=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        code = ai.main(
            stdin=io.StringIO(raw),
            stdout=stdout,
            stderr=stderr,
            client_factory=lambda: client or StubClient(),
        )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_success_writes_only_the_snippet_to_stdout(self) -> None:
        code, out, err = self.run_main(
            {"line": 1, "prompt": "p", "buffer_lines": ["a"]},
            StubClient(response("\\alpha")),
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "\\alpha\n")
        self.assertEqual(err, "")

    def test_invalid_input_exits_nonzero_without_stdout(self) -> None:
        code, out, err = self.run_main({"line": 9, "prompt": "p", "buffer_lines": ["a"]})
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("out of range", err)

    def test_api_failure_exits_nonzero_without_stdout(self) -> None:
        code, out, err = self.run_main(
            {"line": 1, "prompt": "p", "buffer_lines": ["a"]},
            StubClient(response("", refusal="no")),
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("texman ai:", err)

    def test_missing_configuration_exits_nonzero(self) -> None:
        with mock.patch.dict(os.environ, {ai.MODEL_ENV: ""}, clear=False):
            code, out, err = self.run_main({"line": 1, "prompt": "p", "buffer_lines": ["a"]})
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn(ai.MODEL_ENV, err)

    def test_does_not_write_to_any_source_file(self) -> None:
        # The helper has no filesystem writes at all; assert that no open()
        # for writing happens during a full run.
        real_open = open
        opened: list[str] = []

        def watched_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wxa+"):
                opened.append(str(file))
            return real_open(file, mode, *args, **kwargs)

        with mock.patch("builtins.open", watched_open):
            code, _, _ = self.run_main({"line": 1, "prompt": "p", "buffer_lines": ["a"]})
        self.assertEqual(code, 0)
        self.assertEqual(opened, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class KeymapModeTests(unittest.TestCase):
    """`keymap` requests describe Neovim's key state, not a document."""

    def payload(self, **extra) -> str:
        body = {"mode": "keymap", "prompt": "enumerate with the cursor inside"}
        body.update(extra)
        return json.dumps(body)

    def test_needs_no_buffer_or_line(self) -> None:
        request = ai.parse_request(self.payload())
        self.assertEqual(request.mode, ai.MODE_KEYMAP)
        self.assertEqual(request.buffer_lines, [])
        self.assertEqual(request.mapped_keys, ())

    def test_carries_neovim_state(self) -> None:
        request = ai.parse_request(
            self.payload(
                keymap_file_text="-- one",
                mapleader=" ",
                maplocalleader=",",
                mapped_keys=["n <leader>e", "i <Tab>l"],
            )
        )
        self.assertEqual(request.keymap_file_text, "-- one")
        self.assertEqual(request.mapleader, " ")
        self.assertEqual(request.maplocalleader, ",")
        self.assertEqual(request.mapped_keys, ("n <leader>e", "i <Tab>l"))

    def test_rejects_malformed_state(self) -> None:
        for extra in (
            {"keymap_file_text": 3},
            {"mapleader": None},
            {"mapped_keys": "n x"},
            {"mapped_keys": [1]},
        ):
            with self.assertRaises(ai.AiError):
                ai.parse_request(self.payload(**extra))

    def test_prompt_is_still_required(self) -> None:
        with self.assertRaises(ai.AiError):
            ai.parse_request(json.dumps({"mode": "keymap", "prompt": " "}))

    def test_mapped_keys_are_capped(self) -> None:
        keys = [f"n <leader>{n}" for n in range(ai.MAX_MAPPED_KEYS + 50)]
        request = ai.parse_request(self.payload(mapped_keys=keys))
        self.assertEqual(len(request.mapped_keys), ai.MAX_MAPPED_KEYS)

    def test_body_names_the_request_leaders_keys_and_file(self) -> None:
        request = ai.parse_request(
            self.payload(
                keymap_file_text="-- existing mapping",
                mapleader=" ",
                mapped_keys=["i <Tab>l"],
            )
        )
        body = ai.build_input(request)
        self.assertIn("Request: enumerate with the cursor inside", body)
        self.assertIn("mapleader is ' '", body)
        self.assertIn("maplocalleader is unset", body)
        self.assertIn("i <Tab>l", body)
        self.assertIn("-- existing mapping", body)
        self.assertIn("reference only", body)

    def test_empty_file_is_labelled(self) -> None:
        body = ai.build_input(ai.parse_request(self.payload()))
        self.assertIn("(empty)", body)
        self.assertIn("(none reported)", body)

    def test_long_file_keeps_its_tail(self) -> None:
        text = "-- old\n" * 3000 + "-- newest mapping\n"
        request = ai.parse_request(self.payload(keymap_file_text=text))
        body = ai.build_input(request)
        self.assertIn("omitted", body)
        self.assertIn("-- newest mapping", body)
        self.assertLess(len(body), ai.MAX_KEYMAP_FILE_CHARS + 1_500)

    def test_uses_the_keymap_instructions(self) -> None:
        request = ai.parse_request(self.payload())
        instructions = ai.instructions_for(request)
        self.assertIs(instructions, ai.INSTRUCTIONS_KEYMAP)
        self.assertIn("No Markdown", instructions)
        self.assertIn("vim.keymap.set", instructions)
        self.assertIn(ai.KEYMAP_GROUP, instructions)
        self.assertIn("never create or clear an augroup", instructions)
        self.assertIn('"\\\\begin{enumerate}"', instructions)

    def test_generate_returns_lua_and_strips_a_fence(self) -> None:
        code = "vim.keymap.set('n', '<leader>e', 'x', { desc = 'e' })"
        client = StubClient(response(f"```lua\n{code}\n```"))
        with mock.patch.dict(
            os.environ, {ai.MODEL_ENV: "test-model", ai.KEY_ENV: "sk-test"}
        ):
            result = ai.generate(
                ai.parse_request(self.payload()), client_factory=lambda: client
            )
        self.assertEqual(result, code)
        self.assertEqual(client.calls[0]["instructions"], ai.INSTRUCTIONS_KEYMAP)

    def test_empty_output_says_lua(self) -> None:
        client = StubClient(response("   "))
        with mock.patch.dict(
            os.environ, {ai.MODEL_ENV: "test-model", ai.KEY_ENV: "sk-test"}
        ):
            with self.assertRaises(ai.AiError) as caught:
                ai.generate(ai.parse_request(self.payload()), client_factory=lambda: client)
        self.assertIn("no Lua", str(caught.exception))


class RequestOptionTests(unittest.TestCase):
    """`full` and `window`, the two knobs a request carries."""

    def parse(self, **extra: object) -> ai.AiRequest:
        payload = {"line": 1, "prompt": "p", "buffer_lines": ["a"], **extra}
        return ai.parse_request(json.dumps(payload))

    def test_a_request_is_windowed_by_default(self) -> None:
        parsed = self.parse()
        self.assertFalse(parsed.full)
        self.assertEqual(parsed.window, ai.WINDOW_LINES)

    def test_full_is_accepted(self) -> None:
        self.assertTrue(self.parse(full=True).full)

    def test_full_must_be_a_boolean(self) -> None:
        with self.assertRaisesRegex(ai.AiError, "full must be"):
            self.parse(full="yes")

    def test_the_environment_sets_the_window(self) -> None:
        with mock.patch.dict(os.environ, {ai.WINDOW_ENV: "25"}):
            self.assertEqual(self.parse().window, 25)

    def test_an_explicit_window_wins_over_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {ai.WINDOW_ENV: "25"}):
            self.assertEqual(self.parse(window=4).window, 4)

    def test_a_nonsense_window_is_a_clean_error(self) -> None:
        with mock.patch.dict(os.environ, {ai.WINDOW_ENV: "lots"}):
            with self.assertRaisesRegex(ai.AiError, ai.WINDOW_ENV):
                self.parse()

    def test_a_zero_window_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {ai.WINDOW_ENV: "0"}):
            with self.assertRaisesRegex(ai.AiError, "at least 1"):
                self.parse()

    def test_a_blank_window_falls_back_to_the_default(self) -> None:
        with mock.patch.dict(os.environ, {ai.WINDOW_ENV: "  "}):
            self.assertEqual(self.parse().window, ai.WINDOW_LINES)

    def test_a_bad_request_window_is_refused(self) -> None:
        for bad in (0, -3, "8", True):
            with self.subTest(window=bad):
                with self.assertRaisesRegex(ai.AiError, "window must be"):
                    self.parse(window=bad)

    def test_the_window_reaches_the_context(self) -> None:
        buffer = [f"line{i}" for i in range(1, 301)]
        body = ai.build_input(
            ai.AiRequest(line=150, prompt="p", buffer_lines=buffer, window=3)
        )
        self.assertIn("   147| line147", body)
        self.assertNotIn("   146| line146", body)

    def test_a_full_request_says_so_in_the_body(self) -> None:
        buffer = [f"line{i}" for i in range(1, 30)]
        full = ai.build_input(
            ai.AiRequest(line=5, prompt="p", buffer_lines=buffer, full=True)
        )
        self.assertIn("The whole document is shown below.", full)
        windowed = ai.build_input(ai.AiRequest(line=5, prompt="p", buffer_lines=buffer))
        self.assertIn("omitted stretches are labelled", windowed)


class DigestModeTests(unittest.TestCase):
    """The `digest` mode: a preamble in, a summary out."""

    def test_accepts_a_preamble(self) -> None:
        parsed = ai.parse_request(
            json.dumps({"mode": "digest", "preamble_text": "\\usepackage{tikz}"})
        )
        self.assertEqual(parsed.mode, ai.MODE_DIGEST)
        self.assertIn("tikz", parsed.preamble_text)

    def test_needs_no_prompt_or_lines(self) -> None:
        parsed = ai.parse_request(json.dumps({"mode": "digest", "preamble_text": "x"}))
        self.assertEqual(parsed.prompt, "")
        self.assertEqual(parsed.buffer_lines, [])

    def test_rejects_a_missing_or_empty_preamble(self) -> None:
        for bad in ({}, {"preamble_text": ""}, {"preamble_text": 7}):
            with self.subTest(payload=bad):
                with self.assertRaisesRegex(ai.AiError, "preamble_text"):
                    ai.parse_request(json.dumps({"mode": "digest", **bad}))

    def test_the_body_carries_the_preamble(self) -> None:
        body = ai.build_input(
            ai.AiRequest(
                line=0,
                prompt="",
                buffer_lines=[],
                mode=ai.MODE_DIGEST,
                preamble_text="\\usepackage{tikz}",
            )
        )
        self.assertIn("<<<PREAMBLE", body)
        self.assertIn("tikz", body)

    def test_its_own_instructions_are_used(self) -> None:
        digest = ai.AiRequest(
            line=0, prompt="", buffer_lines=[], mode=ai.MODE_DIGEST, preamble_text="x"
        )
        self.assertIs(ai.instructions_for(digest), ai.INSTRUCTIONS_DIGEST)
        self.assertIn("no code fences", ai.INSTRUCTIONS_DIGEST)
        self.assertIn("not instructions", ai.INSTRUCTIONS_DIGEST)

    def test_it_uses_the_mini_model(self) -> None:
        client = StubClient(response("amsmath loaded"))
        digest = ai.AiRequest(
            line=0, prompt="", buffer_lines=[], mode=ai.MODE_DIGEST, preamble_text="x"
        )
        env = {ai.KEY_ENV: "sk-test", ai.MINI_MODEL_ENV: "mini-model"}
        with mock.patch.dict(os.environ, env):
            summary = ai.generate(digest, client_factory=lambda: client)
        self.assertEqual(summary, "amsmath loaded")
        self.assertEqual(client.calls[0]["model"], "mini-model")

    def test_an_unset_mini_model_is_a_clean_error(self) -> None:
        digest = ai.AiRequest(
            line=0, prompt="", buffer_lines=[], mode=ai.MODE_DIGEST, preamble_text="x"
        )
        with mock.patch.dict(os.environ, {ai.KEY_ENV: "sk-test"}):
            os.environ.pop(ai.MINI_MODEL_ENV, None)
            with self.assertRaisesRegex(ai.AiError, ai.MINI_MODEL_ENV):
                ai.generate(digest, client_factory=StubClient)


class PreambleDigestTests(unittest.TestCase):
    """How the cached summary reaches an editing request."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.preamble = Path(self.tmp.name) / "preamble.tex"
        self.preamble.write_text("\\usepackage{amsmath}\n", encoding="utf-8")
        env = {
            "XDG_DATA_HOME": os.path.join(self.tmp.name, "data"),
            documents.PREAMBLE_ENV: str(self.preamble),
            ai.MODEL_ENV: "test-model",
            ai.MINI_MODEL_ENV: "mini-model",
            ai.KEY_ENV: "sk-test",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self) -> "StubClient":
        """A stub that answers the digest and the generation differently."""
        stub = StubClient()

        def create(**kwargs: object) -> SimpleNamespace:
            stub.calls.append(kwargs)
            if kwargs["model"] == "mini-model":
                return response("amsmath loaded -- \\text, \\eqref")
            return response()

        stub.responses.create = create
        return stub

    def test_the_first_request_summarises_and_the_second_does_not(self) -> None:
        stub = self.client()
        ai.generate(request(), client_factory=lambda: stub)
        self.assertEqual([call["model"] for call in stub.calls], ["mini-model", "test-model"])
        stub.calls.clear()
        ai.generate(request(), client_factory=lambda: stub)
        self.assertEqual([call["model"] for call in stub.calls], ["test-model"])

    def test_the_summary_travels_with_the_request(self) -> None:
        stub = self.client()
        ai.generate(request(), client_factory=lambda: stub)
        body = stub.calls[-1]["input"]
        self.assertIn("<<<PREAMBLE", body)
        self.assertIn("amsmath loaded", body)
        self.assertIn("do not repeat any of it", body)

    def test_the_summary_shortens_the_document_preamble_window(self) -> None:
        buffer = [f"line{i}" for i in range(1, 301)]
        stub = self.client()
        ai.generate(
            ai.AiRequest(line=200, prompt="p", buffer_lines=buffer),
            client_factory=lambda: stub,
        )
        body = stub.calls[-1]["input"]
        self.assertIn(f"    {ai.PREAMBLE_LINES_WITH_DIGEST}| ", body)
        self.assertNotIn(f"    {ai.PREAMBLE_LINES_WITH_DIGEST + 1}| ", body)

    def test_a_fix_request_carries_the_summary_too(self) -> None:
        stub = self.client()
        ai.generate(
            ai.AiRequest(
                line=1,
                prompt="Missing }",
                buffer_lines=["bad {"],
                mode=ai.MODE_FIX,
                log_text="./doc.tex:1: Missing } inserted.",
            ),
            client_factory=lambda: stub,
        )
        self.assertIn("amsmath loaded", stub.calls[-1]["input"])

    def test_a_missing_preamble_costs_nothing(self) -> None:
        self.preamble.unlink()
        stub = self.client()
        snippet = ai.generate(request(), client_factory=lambda: stub)
        self.assertEqual([call["model"] for call in stub.calls], ["test-model"])
        self.assertNotIn("<<<PREAMBLE", stub.calls[-1]["input"])
        self.assertTrue(snippet)

    def test_a_failed_summary_still_produces_latex(self) -> None:
        stub = StubClient()

        def create(**kwargs: object) -> SimpleNamespace:
            stub.calls.append(kwargs)
            if kwargs["model"] == "mini-model":
                raise RuntimeError("the summary call fell over")
            return response()

        stub.responses.create = create
        snippet = ai.generate(request(), client_factory=lambda: stub)
        self.assertEqual(snippet, "\\begin{equation}\n  x = 1\n\\end{equation}")
        self.assertNotIn("<<<PREAMBLE", stub.calls[-1]["input"])
        self.assertFalse(preamble.cache_path().exists())

    def test_an_unset_mini_model_does_not_stop_a_generation(self) -> None:
        os.environ.pop(ai.MINI_MODEL_ENV)
        stub = self.client()
        self.assertTrue(ai.generate(request(), client_factory=lambda: stub))
        self.assertEqual([call["model"] for call in stub.calls], ["test-model"])

    def test_an_edited_preamble_is_summarised_again(self) -> None:
        stub = self.client()
        ai.generate(request(), client_factory=lambda: stub)
        self.preamble.write_text("\\usepackage{tikz}\n", encoding="utf-8")
        stub.calls.clear()
        ai.generate(request(), client_factory=lambda: stub)
        self.assertEqual([call["model"] for call in stub.calls], ["mini-model", "test-model"])

    def test_a_keymap_request_never_summarises(self) -> None:
        stub = self.client()
        ai.generate(
            ai.AiRequest(
                line=0, prompt="a shortcut", buffer_lines=[], mode=ai.MODE_KEYMAP
            ),
            client_factory=lambda: stub,
        )
        self.assertEqual([call["model"] for call in stub.calls], ["test-model"])
