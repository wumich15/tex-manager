# texman architecture

`texman` catalogs the `.tex` and `.sty` files already on one person's computer,
lets that person describe and open them, and generates LaTeX fragments inside
Neovim on request. It is a local tool for a single user: no server, daemon,
account, synchronization, or telemetry.

The guiding constraint is that the catalog is a *reference*, never a copy. Files
stay exactly where they are, so relative `\input`, `\include`, image, and style
references keep resolving.

## Components

```text
texman/cli.py     argparse front door: texman, texman scan, texman ai
texman/index.py   directory walk + SQLite persistence (no UI, no network)
texman/tui.py     Textual browser, description editor, scan worker
texman/ai.py      JSON-in/LaTeX-out OpenAI helper (the only network code)
nvim/texman.lua   :TexAI and :TexAIFix; owns all buffer modification
```

Dependencies point one way: `cli` imports the others lazily, `tui` imports
`index`, and nothing imports `ai` except `cli`. `index` and `tui` never import
`openai`, so browsing and describing work with no API key, no network, and no
`openai` package import at all (asserted by a test).

```text
                     ┌────────────┐
  terminal ─────────▶│  cli.py    │
                     └─────┬──────┘
            ┌──────────────┼────────────────┐
            ▼              ▼                ▼
      ┌──────────┐   ┌──────────┐     ┌──────────┐
      │  tui.py  │──▶│ index.py │     │  ai.py   │──▶ OpenAI API
      └────┬─────┘   └────┬─────┘     └────▲─────┘
           │              ▼                │ JSON on stdin
           │      index.sqlite3            │ LaTeX on stdout
           │                               │
           └── suspend() ─▶ nvim ──────────┘
                            └── nvim/texman.lua inserts into the buffer
```

## Data model

One SQLite database at `${XDG_DATA_HOME:-~/.local/share}/texman/index.sqlite3`,
outside the repository, with one table:

```sql
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,       -- absolute, canonical (realpath) identity
    parent_dir TEXT NOT NULL,
    extension TEXT NOT NULL,     -- lowercased, so .TEX and .tex agree
    description TEXT NOT NULL DEFAULT '',
    last_seen TEXT NOT NULL      -- ISO 8601 UTC
);
```

The directory list is `SELECT DISTINCT parent_dir`, so there is no second
directory table to keep consistent. All SQL is parameterized.

The important invariant is that **discovery and description are separate
concerns**. A scan upsert refreshes `parent_dir`, `extension`, and `last_seen`
and its `ON CONFLICT` clause deliberately omits `description`, so no scan can
ever overwrite what the user wrote. Descriptions are committed the moment they
are saved.

Rows are never deleted in this version. A file that disappears keeps its row and
its description and is displayed as unavailable; a stopped or crashed scan
therefore loses nothing. Tracking renames and pruning stale rows are deliberate
omissions.

`TEXMAN_DB` overrides the database path, which is how tests and the `--db` flag
keep off the real catalog.

## Scanning

`index.walk_tree` is a generator over `FileRecord`s with these properties:

- **Metadata only.** It reads names and `stat` results, never file contents, and
  makes no network calls.
- **Default root `/`.** Hidden directories, project folders, `/Volumes`, and
  installed TeX trees are all visited. Only virtual or auto-mounting trees are
  excluded (`/dev`, `/proc`, `/sys`, and the macOS autofs triggers `/net` and
  `/home`), and the excluded roots that actually exist under the scan root are
  reported in the summary. Caches, hidden folders, and system library trees are
  *not* excluded, because they do contain TeX files.
- **No symlink loops.** Directory symlinks are not followed, and visited
  `(st_dev, st_ino)` pairs are remembered so a filesystem alias is walked once.
  File paths are canonicalized with `realpath`, so aliases of one file collapse
  onto one row.
- **Failures are data, not exceptions.** `PermissionError`, vanishing entries,
  and broken links increment `ScanStats.skipped` and contribute up to 20
  representative paths. The walk continues. `sudo` is never required or used.
- **Cooperative cancellation.** `should_cancel()` is polled between directories,
  which bounds stop latency to one `scandir` call.

`ScanStats.complete` is `finished and not cancelled`, and `summary()` says
"stopped" rather than "complete" for a cancelled scan — a stopped scan is
incomplete, not successful. When paths were skipped, both the CLI and the TUI
explain that granting the terminal Full Disk Access on macOS and rescanning
improves coverage, and that unmounted drives cannot be scanned.

`index.run_scan` wraps the walk, upserting in batches (200 rows) inside short
transactions so a concurrent reader sees results as they arrive. It creates its
own connection unless one is passed, which is how the worker thread ends up
owning the connection it writes through.

A measured full scan of `/` on the development machine completed in 42 seconds:
569,833 directories walked, 24,180 files catalogued, 557 paths skipped (macOS
TCC-protected folders such as `~/Library/Caches` subtrees and `~/.Trash`).

## Terminal UI

`TexmanApp` is a Textual app: an `OptionList` of directories on the left (with
"All directories" first), a `DataTable` of files on the right, a detail area
below it, a status line, and a footer of key hints. In the "All directories"
view, files are grouped under bold parent-directory heading rows; heading rows
are tracked separately from file rows, so they cannot be opened or described.

Keys: arrows and Tab to move, `/` to filter, Enter to open in Neovim, `d` to
describe, `r` to rescan, `s` to stop, `q` to quit, Escape to cancel a dialog or
leave the filter. Every letter action returns early when a text input has focus,
so typing `d` into the filter filters rather than opening a dialog.

Scanning uses a Textual thread worker (`@work(thread=True, exclusive=True,
group="scan")`). The worker owns its own SQLite connection, polls
`worker.is_cancelled` between directories, and communicates only through
`post_message`, which is thread-safe. Three messages carry the work back:
`ScanProgress` (counts and current directory), `ScanBatch` (rows committed), and
`ScanFinished` (the final `ScanStats`). Batch messages only set a dirty flag, and a 0.5 s
timer decides whether to redraw. `start_scan` refuses to start a second scan
while one is running.

Redraws are paced by what they actually cost. Rebuilding the table for a
whole-machine catalog (24,180 files under 7,984 directories) takes about half a
second, so the timer waits four times the last measured redraw duration — at
least 0.5 s — before redrawing again. On a fresh catalog that is every 0.5 s and
results stream in; on a full one it is every few seconds, which keeps the event
loop free. The status line updates on every `ScanProgress` message regardless,
because that costs nothing, so progress always looks live. Two further shortcuts
matter at that size: the directory list is rebuilt only when the set of
directories actually changed, and saving a description updates the single table
cell (about 60 ms) instead of redrawing.

Message handlers also run when the DOM is not there — before the widgets mount
and while the app tears down, where a late `RowHighlighted` once crashed the
app. A `_widgets_ready` flag, set in `on_mount` and cleared in `on_unmount`,
makes every handler and redraw a no-op outside that window.

Opening a file uses `App.suspend()` around
`subprocess.run(["nvim", path], cwd=parent_dir)`. The path is a separate
argument list element and no shell is involved, so spaces and special characters
work. After the editor exits, the same row is reselected; the table itself is
not rebuilt, because editing a file changes no catalogued field.

`App.suspend()` resumes application mode after its `with` body but *not* if the
body raises, so an exception escaping it leaves the terminal in the suspended
state with the app still running. Neovim's absence is therefore checked with
`shutil.which` before suspending, and the subprocess call is wrapped in its own
`try` inside the block, with the failure reported after the terminal is back.

## AI helper

`texman ai` is an internal, single-purpose process with a small contract:

- **Input:** one JSON object on stdin — `line` (one-based int), `prompt`
  (nonempty string), `buffer_lines` (array of strings). `line` must be within
  `1 … len(buffer_lines) + 1`.
- **Output:** on success, only the LaTeX fragment on stdout, exit 0.
- **Failure:** invalid input, missing configuration, API error, refusal,
  incomplete response, or empty output all exit nonzero with a short message on
  stderr and nothing on stdout.
- It never writes to a source file. Insertion belongs to Neovim.

Configuration comes from the environment: `OPENAI_API_KEY` and
`TEXMAN_OPENAI_MODEL`, an explicit model ID rather than a moving alias. The
client is constructed only inside a generation request, with a 60-second timeout
and `max_retries=0`; the user retries explicitly.

Context is bounded and built only from the buffer the user is editing — no other
indexed file is read and `\input` is not followed. Up to the first 100 lines
supply packages and macros, and up to 40 lines on each side of the insertion
point supply local conventions. Overlapping ranges are merged, omitted stretches
are labelled (`[... lines 101-109 omitted ...]`), and the insertion point is
marked in place. If the result exceeds 24,000 characters, the preamble is shed
first and the lines nearest the insertion point are kept longest.

The instructions require a bare LaTeX fragment: no Markdown, no prose, no
repeated surrounding text, no document wrapper unless asked, matching
surrounding conventions, and a brief `% Requires \usepackage{...}` comment
instead of editing the preamble. Buffer text is labelled as reference material,
not instructions.

Before the output is used, `status` is checked for `incomplete`, the output
items are checked for a refusal part, a single surrounding Markdown fence is
removed, and empty output is rejected. SDK exceptions are mapped onto short
messages for bad keys, unavailable models, rate limits, timeouts, and connection
failures. Error text is derived from the exception type, never from the key or
the request body.

Fix mode swaps in a second set of instructions: repair the one blamed line,
change as little as possible, return the line unchanged if the log does not
actually implicate it. The request body quotes the failing line verbatim, adds a
bounded excerpt of the compiler log, and marks that line in the document context
rather than marking an insertion point.

A LaTeX log is mostly font and package chatter, so `extract_log_excerpt` keeps
only lines matching error markers -- `!`, `file:line:`, `l.<n>`, `LaTeX Error:`,
`Emergency stop`, `Runaway argument`, `<inserted text>` -- plus six lines after
each, which is where TeX echoes the offending source. Warnings are added only if
the result is still under 6,000 characters; when it is not, warnings go first and
then the latest errors, because in TeX the earliest error is the real one.
Omitted stretches are labelled. A real 6,735-character log reduces to about
1,550 characters this way.

Generation is the only network operation in the program, and it sends only the
prompt, the bounded buffer context, and (for a fix) the bounded log excerpt. The
catalog and descriptions stay local.

## Neovim integration

`nvim/texman.lua` registers `:TexAI <line> <prompt>` with
`nvim_create_user_command`. An uppercase initial is required for user commands,
and it leaves `/` alone so ordinary search is unaffected. If the name is already
taken, `setup` reports the conflict and returns `false`;
`require('texman').setup({ command = 'OtherName' })` picks another.

One request per buffer at a time, tracked in a table keyed by buffer handle and
cleared on every exit path, including failures.

The request captures the buffer handle, its `changedtick`, and its *in-memory*
lines, so unsaved edits are part of the context. `vim.system({'texman','ai'}, …)`
runs asynchronously with the JSON on stdin and the environment inherited; there
is no shell command string, so neither the key nor the prompt is ever exposed to
shell quoting.

The completion callback is scheduled onto the main loop and re-validates before
touching anything: the buffer must still be loaded, still modifiable, and have
the same `changedtick`. Otherwise the result is discarded with an explanation to
rerun — a stale line number is never used. Switching windows in the meantime is
fine, because insertion targets the captured handle rather than the current
buffer.

`:TexAIFix` reuses all of that plumbing and adds the log work. It locates the log
from vimtex's own `b:vimtex.root` and `b:vimtex.compiler.file_info.jobname`
(honouring `out_dir`) when vimtex is loaded, and otherwise tries `<stem>.log`,
`build/<stem>.log`, and `out/<stem>.log` beside the file.

`scan_log` then finds the first error, trying four log shapes in order of how
precisely they name a line: `file:line: message` (from `-file-line-error`, which
vimtex passes); `! message` followed by `l.<n>`; `! message` mentioning `on input
line <n>`, as an unclosed environment does; and finally an unattributed fatal
error located through the `Runaway argument?` text, which is how an unclosed
brace usually appears -- TeX names no line there, but it does echo the source it
swallowed, so the line is found by comparing with whitespace removed. The last
three name no file, so they are only trusted when the log blames no file
anywhere; if the first error belongs to another file, the command says which and
edits nothing.

Three refusals keep a wrong edit from happening. The buffer must be saved,
because log line numbers describe the file on disk. The blamed line must exist in
the buffer. And when the compile failed but nothing can be located, the command
reports the failure instead of claiming the log is clean -- a silent "no errors"
on a build that produced no PDF would be the worst outcome.

Insertion is one `nvim_buf_set_lines(buf, L - 1, L - 1, true, lines)` call, so it
is a single undoable change; a fix is the same call over `L - 1` to `L`. Writing `undolevels` back to itself immediately
before that call syncs undo, which keeps the insertion from merging into the
user's previous edit: one `u` removes the snippet and nothing else. The file is
never saved automatically.

## Verification

- `python -m unittest discover -s tests -t .` — 101 checks. `test_index.py`
  builds a temporary fixture with nested files, hidden folders, duplicate
  filenames, spaces, an uppercase extension, a symlink loop, a broken link, and
  a `chmod 000` directory, and covers description persistence across restarts,
  rescans, cancelled scans, and deleted files. `test_ai.py` stubs the client, so
  no test makes a paid API call. `test_tui.py` drives the app headlessly with
  Textual's pilot, stubbing `suspend()` and `subprocess.run`, and covers late
  messages arriving after teardown. `test_cli.py` runs
  the real command as a subprocess, including `texman ai` failure paths that
  need no API key.
- `nvim --headless -u NONE -l tests/test_nvim.lua` — 58 checks against a fake
  `texman` executable on `PATH`, covering insertion before the first and a
  middle line, appending, invalid lines and prompts, unsaved context,
  backslashes in prompts, one-step undo, switched buffers, changed and closed
  buffers, duplicate requests, helper failure, empty output, and a missing
  helper.
- Automated tests only ever scan a temporary fixture, and they always stub the
  client. Scanning `/` and one live generation request are manual checks,
  recorded in the README.

## Deliberate omissions

No server, daemon, filesystem watcher, ORM, vector database, agent framework,
document parser, streaming protocol, or automatic LaTeX compiler. Manual refresh
and one API request per prompt are enough. Also deferred: Windows support and
installer packaging, automatic deletion of stale rows, rename tracking, bulk
actions, tags, favorites, file operations, and configurable scan exclusions.
