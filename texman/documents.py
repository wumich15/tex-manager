"""The preamble template and new documents made from it.

`preamble.tex` is a plain file the user edits in Neovim; a new document is that
file's text with a document body appended, written where the user asked. This
module never touches the catalog, so it needs no database connection.
"""

from __future__ import annotations

import os
from pathlib import Path

PREAMBLE_ENV = "TEXMAN_PREAMBLE"

DEFAULT_PREAMBLE = """\
% texman preamble template.
%
% Every document created with `n` in texman starts with a copy of this file.
% Keep the preamble here; \\begin{document} ... \\end{document} are added to
% each new document automatically unless this file contains them itself.
\\documentclass[11pt]{article}

\\usepackage[utf8]{inputenc}
\\usepackage[T1]{fontenc}
\\usepackage{amsmath, amssymb, amsthm}
\\usepackage{graphicx}
\\usepackage{hyperref}
"""

DOCUMENT_BODY = """\

\\begin{document}

\\end{document}
"""


class DocumentError(Exception):
    """A short, actionable reason a document could not be created."""


def config_dir() -> Path:
    """Return the per-user configuration directory, honouring XDG_CONFIG_HOME."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / "texman"


def default_preamble_path() -> Path:
    """Where the template lives: `$TEXMAN_PREAMBLE`, or the config directory."""
    override = os.environ.get(PREAMBLE_ENV)
    if override:
        return Path(os.path.expanduser(override))
    return config_dir() / "preamble.tex"


def ensure_preamble(path: str | os.PathLike[str]) -> bool:
    """Create the template with default contents if it is missing.

    Returns True when the file was created, False when it already existed. An
    existing file is never rewritten: it is the user's own preamble.
    """
    target = Path(path)
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "x", encoding="utf-8") as handle:
        handle.write(DEFAULT_PREAMBLE)
    return True


def read_preamble(path: str | os.PathLike[str]) -> str | None:
    """Return the template text, or None if there is no template yet."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DocumentError(f"could not read the preamble at {path}: {exc}") from exc


def render_document(preamble: str) -> str:
    """Turn the template into a complete document.

    A template that already contains `\\begin{document}` is used verbatim, so
    a user who prefers a whole skeleton can keep one. Otherwise the body is
    appended, leaving one empty line for the cursor.
    """
    if "\\begin{document}" in preamble:
        return preamble if preamble.endswith("\n") else preamble + "\n"
    return preamble.rstrip("\n") + "\n" + DOCUMENT_BODY


def document_name(name: str) -> str:
    """Validate a file name typed by the user and give it a `.tex` extension."""
    cleaned = name.strip()
    if not cleaned:
        raise DocumentError("the document needs a name")
    if os.sep in cleaned or (os.altsep and os.altsep in cleaned):
        raise DocumentError(
            "the name must not contain a path separator; put the directory in "
            "the directory field"
        )
    if cleaned in (".", ".."):
        raise DocumentError(f"{cleaned!r} is not a file name")
    if not cleaned.lower().endswith(".tex"):
        cleaned += ".tex"
    return cleaned


def resolve_directory(directory: str) -> str:
    """Canonicalise the directory the user typed, expanding `~`."""
    cleaned = directory.strip()
    if not cleaned:
        raise DocumentError("the document needs a directory")
    expanded = os.path.realpath(os.path.expanduser(cleaned))
    if os.path.exists(expanded) and not os.path.isdir(expanded):
        raise DocumentError(f"{expanded} exists and is not a directory")
    return expanded


def create_document(
    directory: str,
    name: str,
    preamble_path: str | os.PathLike[str],
) -> tuple[str, bool]:
    """Write a new document from the template and return its path.

    The second value says whether the template existed: when it did not, the
    built-in default preamble was used so the user is not left with an empty
    file, and the caller should say so.

    A missing directory is created. An existing file is never overwritten.
    """
    target_dir = resolve_directory(directory)
    file_name = document_name(name)
    path = os.path.join(target_dir, file_name)
    template = read_preamble(preamble_path)
    text = render_document(DEFAULT_PREAMBLE if template is None else template)
    try:
        os.makedirs(target_dir, exist_ok=True)
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError as exc:
        raise DocumentError(f"{path} already exists; choose another name") from exc
    except OSError as exc:
        raise DocumentError(f"could not create {path}: {exc}") from exc
    return path, template is not None
