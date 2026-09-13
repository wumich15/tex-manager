# tex-manager: working notes for `texman`

`texman` is a personal LaTeX file manager: a terminal catalog of the `.tex` and
`.sty` files already on the machine, a description per file, Neovim as the
editor, and a `:TexAI <line> <prompt>` command that inserts generated LaTeX.

**The plan in this file has been implemented.** Everything in
"What the first version does" works and is verified as described under
"Verifying changes". Read [ARCHITECTURE.md](ARCHITECTURE.md) before changing
anything non-trivial; it explains the design decisions and the invariants below.
[README.md](README.md) is the user-facing install and usage guide.

## What the first version does

1. `texman` opens a terminal UI and scans the computer for `.tex` and `.sty`
   files, showing their containing directories and files in one searchable
   catalog, grouped by directory.
2. The user can select a file, add or edit a description, see that description in
   the terminal, and open the file in Neovim. Descriptions survive restarts and
   rescans.
3. While editing a TeX file in Neovim, `:TexAI <line> <prompt>` inserts generated
   LaTeX before that line, for example
   `:TexAI 25 Add a TikZ diagram of a three-node directed cycle`.
   `:TexAIFix` reads the compiler log, finds the line LaTeX blamed, and replaces
   it with a corrected version. This was added after the first version shipped,
   at the user's request; it is part of the intended scope now.
4. Snippets are generated through the OpenAI API using the user's own API key.
5. Everything is suitable for one person's local use. No accounts, hosted
   backend, synchronization, telemetry, or multi-user features.

"One place" means a catalog of the original files. Do not move or copy them:
relative `\input`, `\include`, images, and style references must keep working.

## Layout

```text
pyproject.toml            # setuptools package; texman = "texman.cli:main"
texman/
  __init__.py             # version only
  cli.py                  # texman, texman scan, internal texman ai
  index.py                # filesystem traversal and SQLite persistence
  tui.py, tui.tcss        # directory/file browser and description editor
  ai.py                   # request validation and OpenAI snippet generation
nvim/texman.lua           # :TexAI command
tests/
  test_index.py           # walk + catalog, against a temporary fixture
  test_ai.py              # request contract, with a stubbed client
  test_tui.py             # headless Textual pilot, with nvim stubbed
  test_cli.py             # the installed command, as a real subprocess
  test_nvim.lua           # headless Neovim, with a fake texman on PATH
ARCHITECTURE.md, README.md
```

The catalog is one SQLite database at
`${XDG_DATA_HOME:-~/.local/share}/texman/index.sqlite3`, outside this
repository. `TEXMAN_DB` and `--db` point it elsewhere; tests always do.

## Environment

Target macOS first, matching the development machine. Keep filesystem code
portable to Linux. Windows support and installer packaging are still deferred.

- Python 3.11+; `textual` and `openai` are the only direct runtime dependencies.
- Standard-library `argparse`, `pathlib`/`os`, `sqlite3`, `json`, and
  `subprocess` for everything else; `unittest` for tests.
- A single Lua file for Neovim 0.10+, using built-in process and buffer APIs. No
  Python Neovim provider or plugin framework.

## Invariants to preserve

These are the rules that keep the tool honest. Breaking one is a bug even if the
tests still pass.

- **A scan never overwrites a description.** The upsert's `ON CONFLICT` clause
  updates discovery fields only. Descriptions commit as soon as they are saved.
- **Rows are never deleted.** A missing file keeps its row and description and
  displays as unavailable. A stopped or interrupted scan loses nothing.
  Automatic deletion and rename tracking remain out of scope.
- **Identities are absolute canonical paths**, extensions are matched
  case-insensitively and stored lowercased, and all SQL is parameterized.
- **Discovery reads metadata only** — no file contents, no network. The scan
  root defaults to `/`.
- **Only virtual and auto-mounting trees are excluded** (`/dev`, `/proc`, `/sys`,
  `/net`, `/home`), and the summary names the ones that applied. Do not broadly
  exclude system libraries, hidden folders, or caches: they contain TeX files.
- **Traversal tolerates failure.** Directory symlinks are not followed, visited
  device/inode pairs are remembered, permission errors and vanishing entries are
  counted with representative paths, and the walk continues. Never require
  `sudo` or change filesystem permissions. Report partial coverage honestly, and
  mention Full Disk Access on macOS when paths were skipped.
- **A stopped scan is incomplete, not successful** — in the summary text, in
  `ScanStats.complete`, and in the CLI exit status.
- **No overlapping scans.** One Textual thread worker at a time, cancelled
  cooperatively between directories. SQLite connections stay owned by the thread
  that created them; writes are batched in short transactions with a busy
  timeout.
- **Letter shortcuts never fire while a text input has focus.**
- **The UI stays responsive on a whole-machine catalog.** Redraws during a scan
  are paced by the last measured redraw cost (four times it, at least 0.5 s);
  the directory list is rebuilt only when the directory set changed; saving a
  description updates one table cell rather than redrawing. Message handlers
  check `_widgets_ready`, because they also run before mount and during
  teardown.
- **Neovim is launched as an argument list**, never a shell string, with the
  file's parent directory as the working directory, inside `App.suspend()`.
  Nothing may raise *through* that `with` block: `App.suspend()` resumes
  application mode after the body but not on an exception, so an escaping error
  leaves the user's terminal unusable. Check for the executable first and catch
  subprocess errors inside the block.
- **A failing scan must not crash or wedge the app.** The worker catches
  everything, always reports a result, and clears the running flag, so `r`
  works again afterwards.
- **Bad input gets a clean message, never a traceback**: a scan root that is
  missing or not a directory, and a `--db` file that is not a SQLite database.
- **Never silently drop something the user typed.** `set_description` reports
  whether a row matched, and the UI says so if the write found nothing.
- **`texman ai` never modifies a file.** It reads one JSON object on stdin,
  writes only the snippet to stdout on success, and exits nonzero with a short
  stderr message on invalid input, missing configuration, API failure, refusal,
  incomplete response, or empty output.
- **The client is constructed only for a generation request**, so browsing and
  descriptions work with no API key, no network, and no `openai` import. Both
  `OPENAI_API_KEY` and `TEXMAN_OPENAI_MODEL` come from the environment; the
  model is an explicit ID, not a moving alias. One request, a 60-second timeout,
  no automatic retries, explicit user retry.
- **Context is bounded**: up to the first 100 lines, up to 40 lines each side of
  the insertion point, overlap merged, omissions labelled, 24,000 characters
  total, nearby text kept longest. Never read other indexed files or follow
  `\input`. Buffer text is document context, not instructions.
- **Never display or log the key or the full request body.**
- **`:TexAIFix` must never make a wrong edit.** It refuses an unsaved buffer,
  because log line numbers describe the file on disk. It refuses when the first
  error belongs to another file, naming that file. It refuses a blamed line
  outside the buffer. When a compile failed but no line can be located, it
  reports the failure rather than claiming the log is clean -- a silent "no
  errors" on a build that produced no PDF is the worst possible outcome. It
  replaces exactly the blamed line, in one undoable change, and saves nothing.
- **All four LaTeX error shapes are handled**, in order of precision:
  `file:line: message` (from `-file-line-error`, which vimtex passes), `!
  message` plus `l.<n>`, `! message` mentioning `on input line <n>`, and an
  unattributed fatal error located through the `Runaway argument?` text that TeX
  echoes back. The last three name no file, so trust them only when the log
  blames no file anywhere. Do not loosen the eight-character minimum on runaway
  matching: a short echo would match the wrong line.
- **Neovim owns insertion.** `:TexAI` inserts *before* line `L`; valid values are
  `1` through `N + 1`, where `N + 1` appends. The request captures the buffer
  handle, its `changedtick`, and its in-memory lines; the scheduled callback
  re-checks that the buffer is loaded, modifiable, and unchanged before a single
  `nvim_buf_set_lines` call, so the insertion is one undo step and never lands on
  a stale line. One active request per buffer, with the flag cleared on every
  exit path. Nothing is saved automatically.
- The only network operation is the explicitly requested AI generation. The
  catalog and saved descriptions stay local.

## Verifying changes

```sh
python -m unittest discover -s tests -t .       # 127 checks
nvim --headless -u NONE -l tests/test_nvim.lua  # 107 checks
```

Automated tests use only a temporary fixture — never the developer's whole
machine — and always stub the OpenAI client, so they make no paid API calls.
The fixture covers nested files, hidden folders, duplicate filenames, spaces,
an uppercase extension, a symlink loop, a broken link, and a permission failure.
Note that `Custom.Sty` and `custom.sty` are the same file on macOS, so
case-variant fixture names must live in different directories.

Manual checks, when touching the relevant area:

- A real scan from `/` stays responsive, reports inaccessible paths, and groups
  files under their directories. Last measured: 42 s, 569,833 directories,
  24,180 files, 557 skipped. With that catalog cached and a full scan running,
  the UI's median event-loop tick stayed at 0.10 s with a 1.5 s worst-case
  hiccup during a table rebuild.
- `texman --help` works from a directory other than the repository.
- A path containing spaces opens in Neovim and returns to the TUI.
- File management works with both API variables unset.
- One real `:TexAI` request with a configured key: the LaTeX appears at the
  requested position, unrelated text stays intact, `u` undoes it, and the file
  on disk changes only after `:write`. **Done** — verified against a live
  project with `TEXMAN_OPENAI_MODEL=gpt-5.6-luna`; see the README. Mocked output
  alone never proves API connectivity, so redo this check by hand if the request
  path changes.

## Do not add

No server, daemon, filesystem watcher, ORM, vector database, agent framework,
document parser, streaming protocol, or automatic LaTeX compiler. Manual refresh
and one API request per prompt are enough. `:TexAIFix` *reads* a log the user's
own compiler already produced; it never runs the compiler itself.

Also deferred on purpose: bulk actions, tags, favorites, file operations,
configurable scan exclusions, stale-row pruning, rename tracking, Windows
support, and installer packaging. `:TexAIFix` fixes one line from one error; it
does not iterate, recompile, or repair a whole document. The complete personal
workflow works; do not expand scope further without being asked.
