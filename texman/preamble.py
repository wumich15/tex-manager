"""The cached digest of the shared preamble template.

Every document made with `n` starts from one `preamble.tex`, so its packages and
macros are the conventions a generated snippet has to match. Sending that file
with every request would be wasteful and would crowd out the document itself, so
it is summarised once by a cheap model and the summary is cached here. The cache
is keyed on the file's contents: edit `preamble.tex` and the next request
refreshes it, leave it alone and nothing is re-read and nothing is re-sent.

This module knows nothing about OpenAI. `ensure_digest` takes the function that
produces a digest, so the caller owns the request and tests can stub it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import index

CACHE_NAME = "preamble-digest.json"

# The digest describes a preamble; a preamble longer than this is pathological,
# and its head is the part that carries the class and the packages.
MAX_PREAMBLE_CHARS = 20_000


class DigestError(Exception):
    """A short, actionable reason the preamble could not be digested."""


@dataclass(frozen=True)
class Digest:
    """A cached summary of one preamble file."""

    path: str
    sha256: str
    size: int
    model: str
    text: str
    generated_at: str


def cache_path() -> Path:
    """Where the digest is cached, beside the catalog and outside the repo."""
    return index.data_dir() / CACHE_NAME


def read_preamble(path: str | os.PathLike[str]) -> str:
    """Return the preamble text, raising a clean error if it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except FileNotFoundError as exc:
        raise DigestError(
            f"there is no preamble at {path}; create one with `p` in texman"
        ) from exc
    except OSError as exc:
        raise DigestError(f"could not read the preamble at {path}: {exc}") from exc


def fingerprint(text: str) -> str:
    """Identify a preamble by its contents, so an edit invalidates the cache."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load(preamble_path: str | os.PathLike[str], text: str | None = None) -> Digest | None:
    """Return the cached digest for this preamble, or None for a miss.

    A cache that is absent, unreadable, malformed, or describes a different file
    or different contents is a miss, never an error: a stale or corrupt cache
    must degrade to "generate it again", never break a request.
    """
    try:
        raw = cache_path().read_text(encoding="utf-8")
        record = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None

    wanted = os.path.realpath(os.path.expanduser(str(preamble_path)))
    if record.get("path") != wanted:
        return None
    if text is None:
        try:
            text = read_preamble(wanted)
        except DigestError:
            return None
    if record.get("sha256") != fingerprint(text):
        return None

    digest_text = record.get("digest")
    if not isinstance(digest_text, str) or not digest_text.strip():
        return None
    return Digest(
        path=wanted,
        sha256=str(record.get("sha256", "")),
        size=int(record.get("size", 0) or 0),
        model=str(record.get("model", "")),
        text=digest_text,
        generated_at=str(record.get("generated_at", "")),
    )


def store(
    preamble_path: str | os.PathLike[str],
    text: str,
    digest_text: str,
    model: str,
) -> Digest:
    """Write the digest atomically, so a crash never leaves half a cache."""
    wanted = os.path.realpath(os.path.expanduser(str(preamble_path)))
    record = Digest(
        path=wanted,
        sha256=fingerprint(text),
        size=len(text),
        model=model,
        text=digest_text.strip(),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    target = cache_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=CACHE_NAME + ".",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(
                    {
                        "path": record.path,
                        "sha256": record.sha256,
                        "size": record.size,
                        "model": record.model,
                        "digest": record.text,
                        "generated_at": record.generated_at,
                    },
                    handle,
                    indent=2,
                )
                handle.write("\n")
            os.replace(handle.name, target)
        except BaseException:
            # A failed write must not leave the temporary file behind.
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise DigestError(f"could not write the digest cache at {target}: {exc}") from exc
    return record


def clip(text: str, max_chars: int = MAX_PREAMBLE_CHARS) -> str:
    """Bound the preamble sent for digesting, keeping the head."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n% [... rest of the preamble omitted ...]"


def ensure_digest(
    preamble_path: str | os.PathLike[str],
    generate: Callable[[str], tuple[str, str]],
    *,
    force: bool = False,
) -> tuple[Digest, bool]:
    """Return the digest for this preamble, generating it only when stale.

    `generate` receives the (clipped) preamble text and returns the digest and
    the model that produced it. The second return value says whether a request
    was actually made, so the caller can tell the user which happened.
    """
    text = read_preamble(preamble_path)
    if not force:
        cached = load(preamble_path, text)
        if cached is not None:
            return cached, False
    digest_text, model = generate(clip(text))
    if not digest_text.strip():
        raise DigestError("the model returned an empty preamble digest; try again")
    return store(preamble_path, text, digest_text, model), True
