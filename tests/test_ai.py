"""Checks for the `texman ai` contract.

The OpenAI client is always a stub, so these tests make no paid API calls and
need neither a key nor a network connection.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from texman import ai


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


class ContextTests(unittest.TestCase):
    def test_includes_preamble_and_both_windows(self) -> None:
        buffer = [f"line{i}" for i in range(1, 301)]
        context = ai.build_context(buffer, 150)
        self.assertIn("line1", context)
        self.assertIn("line100", context)
        self.assertIn("line110", context)  # 40 lines before the insertion point
        self.assertIn("line189", context)  # 40 lines after it
        self.assertNotIn("line105", context)
        self.assertNotIn("line200", context)

    def test_marks_the_insertion_point(self) -> None:
        context = ai.build_context(["a", "b", "c"], 2)
        self.assertIn("INSERT THE NEW LATEX HERE, before line 2", context)
        self.assertLess(context.index("a"), context.index("INSERT"))
        self.assertGreater(context.index("b"), context.index("INSERT"))

    def test_labels_omitted_sections(self) -> None:
        buffer = [f"line{i}" for i in range(1, 301)]
        context = ai.build_context(buffer, 150)
        self.assertIn("[... lines 101-109 omitted ...]", context)
        self.assertIn("[... lines 190-300 omitted ...]", context)

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
        self.assertIn("[... lines 1-", context)

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
