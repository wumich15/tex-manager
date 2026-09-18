# texman architecture

`texman` catalogs the `.tex` and `.sty` files already on one person's computer,
lets that person describe, open, and create them, and generates LaTeX fragments
and Neovim key mappings inside Neovim on request. It is a local tool for a single user: no server, daemon,
account, synchronization, or telemetry.

The guiding constraint is that the catalog is a *reference*, never a copy. Files
stay exactly where they are, so relative `\input`, `\include`, image, and style
references keep resolving.

## Components

```text
texman/cli.py        argparse front door: texman, texman scan/ai/preamble
texman/index.py      directory walk + SQLite persistence (no UI, no network)
texman/documents.py  the preamble.tex template and documents made from it
texman/preamble.py   the cached summary of that template (no network of its own)
texman/tui.py        Textual browser, description editor, scan worker
texman/ai.py         JSON-in/text-out OpenAI helper (the only network code)
nvim/texman.lua      :TexAI, :TexAIFix, :TexAIMap, :TexAIPrompt, :TexPreamble; owns all
                     buffer modification
```

Dependencies point one way: `cli` imports the others lazily, `tui` imports
`index` and `documents`, and nothing imports `ai` except `cli`. `index` and `tui` never import
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
           │      index.sqlite3            │ LaTeX or Lua on stdout
           │                               │
           └── suspend() ─▶ nvim ──────────┘
                            └── nvim/texman.lua inserts into the buffer
```

## Data model

One SQLite database at `${XDG_DATA_HOME:-~/.local/share}/texman/index.sqlite3`,
outside the repository, with two tables:

```sql
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,       -- absolute, canonical (realpath) identity
    parent_dir TEXT NOT NULL,
    extension TEXT NOT NULL,     -- lowercased, so .TEX and .tex agree
    description TEXT NOT NULL DEFAULT '',
    last_seen TEXT NOT NULL      -- ISO 8601 UTC
);
```

```sql
CREATE TABLE IF NOT EXISTS ignored_dirs (
    path TEXT PRIMARY KEY,       -- absolute directory the user chose to hide
    ignored_at TEXT NOT NULL     -- ISO 8601 UTC
);
```

The directory list is `SELECT DISTINCT parent_dir`, so there is no second
directory table to keep consistent. All SQL is parameterized. Both tables are
created with `IF NOT EXISTS` on every `connect`, so an existing catalog gains
`ignored_dirs` without a migration step.

The important invariant is that **discovery and description are separate
concerns**. A scan upsert refreshes `parent_dir`, `extension`, and `last_seen`
and its `ON CONFLICT` clause deliberately omits `description`, so no scan can
ever overwrite what the user wrote. Descriptions are committed the moment they
are saved.

Rows are never deleted in this version. A file that disappears keeps its row and
its description and is displayed as unavailable; a stopped or crashed scan
therefore loses nothing. Tracking renames and pruning stale rows are deliberate
omissions.

**Ignoring is a view, not a deletion.** An entry in `ignored_dirs` hides a
directory and everything beneath it: `covering_ignore` compares whole path
segments, so `/a/b` hides `/a/b/c` but not `/a/bc`. The catalog queries filter
on that prefix in Python rather than in SQL, because prefix matching is path
semantics and the ignore list is short — a handful of entries against a few
hundred directory groups. `list_files(under=...)` inverts the same rule, which
is how a selected ignored directory shows the tree it is hiding. Nothing is
removed, so restoring a directory brings back every row and every description
exactly as they were. One entry covers a whole tree, so ignoring a nested
directory whose parent is already ignored is refused and reported rather than
recorded; unignoring such a directory is refused too, naming the parent that
actually hides it.

`TEXMAN_DB` overrides the database path, which is how tests and the `--db` flag
keep off the real catalog.

## Scanning

`index.walk_tree` is a generator over `FileRecord`s with these properties:

- **Metadata only.** It reads names and `stat` results, never file contents, and
  makes no network calls.
- **Several roots, defaulting to `~/Documents` and `~/Downloads`.** A walk takes
  a list of roots and shares one visited set across them, so a file reachable
  from two roots is still yielded once. The defaults are the directories a
  person keeps their own work in; scanning from `/` instead buried them under
  tens of thousands of TeX Live package files, so `--root` (repeatable) is how
  you ask for anything wider. A default root that does not exist is dropped;
  a root you name explicitly must exist, or the command says so and stops.
  Within a root nothing is filtered: hidden directories, project folders, and
  installed TeX trees are all visited. Only virtual or auto-mounting trees are
  excluded (`/dev`, `/proc`, `/sys`, and the macOS autofs triggers `/net` and
  `/home`), and the excluded roots that actually exist under a scan root are
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
describe, `n` to create a document, `p` to edit the preamble template, `i` to
ignore or restore a directory, `r` to rescan, `s` to stop, `q` to quit, Escape
to cancel a dialog or leave the filter. Every letter action returns early when a
text input has focus, so typing `d` into the filter filters rather than opening
a dialog.

### Vim-style navigation

`j`, `k`, `gg`, `G`, `h`, `l`, and the two-key sequences `;s` (next directory)
and `;a` (previous directory) take an optional count first, so `5j` moves five
rows and `2;s` skips a directory. They are implemented in one `on_key` handler
on the app rather than as bindings, because a count and a prefix are state: the
handler keeps the typed digits and a pending `;` or `g`, and consumes each key
with `prevent_default()` plus `stop()`. Textual dispatches the subclass's
`on_key` before `App._on_key`, which is where bindings are resolved, so a
consumed key never reaches them -- the `s` of `;s` must not stop a scan. Arrow
keys are consumed only when a count is pending; otherwise the widgets keep
their own handling. The handler returns early, clearing its state, while an
`Input` or a modal dialog has focus, so digits and letters type normally there.
A non-motion key drops a pending count and proceeds to its binding, as Vim
does; an unknown sequence after `;` or `g` is reported and does nothing. A
`semicolon` binding still exists on the app, unreachable by keyboard, so the
footer can advertise `;s`.

"Next directory" depends on what the table shows. In the grouped
"All directories" view it moves the cursor to the first file after the next
heading row. When a single directory is selected, or the directory pane has
focus, it steps the directory list's highlight instead, skipping the disabled
`── ignored ──` separator, and the table follows through the usual
`OptionHighlighted` message.

### The preamble and new documents

`documents.py` owns the template, `${XDG_CONFIG_HOME:-~/.config}/texman/preamble.tex`
(overridden by `TEXMAN_PREAMBLE` or `--preamble`), and the creation of files
from it, with no database or UI involvement. `p` creates the template with a
starter preamble only if it is missing -- an existing one is the user's own and
is never rewritten -- and opens it in Neovim through the same suspend path as a
catalogued file. `n` opens a dialog with a file name and a directory, the latter
prefilled from the same "directory in context" rule as `i`, falling back to the
first scan root. `create_document` appends `.tex` when missing, refuses path
separators in the name, expands `~`, creates a missing directory, and opens the
file with mode `x` so an existing file is never overwritten. The text is the
template followed by `\begin{document}`/`\end{document}` unless the template
already contains `\begin{document}`, in which case it is used verbatim. With no
template at all the built-in default is used and the notification says so. The
new file is upserted into the catalog at once, selected, and opened in Neovim,
so it needs no rescan to appear.

`i` acts on "the directory in context": the highlighted entry when the directory
pane has focus, otherwise the directory of the selected table row (a heading row
names one directly, a file row through its parent). The table is where the
cursor normally sits, so binding the key to only one pane would have made the
common case awkward. Ignored directories stay listed, below a disabled `──
ignored ──` separator and labelled with how many files they hide, because a
directory that vanished completely would be one nobody could restore. Selecting
one shows its files anyway: the ignore filter applies to the aggregate view,
while a directory asked for by name always shows itself.

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

- **Input:** one JSON object on stdin — `mode` (`insert`, the default, `fix`,
  `keymap`, or `digest`), `prompt` (nonempty string), and for the two document
  modes `line` (one-based int) and `buffer_lines` (array of strings), with
  `line` within `1 … len(buffer_lines) + 1` for an insert and within the buffer
  for a fix. Document modes also take `full` (boolean) and `window` (positive
  int). A `keymap` request carries Neovim state instead: `keymap_file_text`,
  `mapleader`, `maplocalleader`, and `mapped_keys`. A `digest` request carries
  only `preamble_text`, and has no prompt.
- **Output:** on success, only the generated fragment on stdout -- LaTeX, Lua
  for a mapping, or plain text for a digest -- exit 0.
- **Failure:** invalid input, missing configuration, API error, refusal,
  incomplete response, or empty output all exit nonzero with a short message on
  stderr and nothing on stdout.
- It never writes to a source file. Insertion belongs to Neovim. The one file it
  writes at all is its own preamble-digest cache, described below.

Configuration comes from the environment: `OPENAI_API_KEY` and
`TEXMAN_OPENAI_MODEL`, an explicit model ID rather than a moving alias, plus
`TEXMAN_OPENAI_MINI_MODEL` for the preamble digest and `TEXMAN_WINDOW_LINES`
for the context window. The client is constructed only inside a generation
request, with a 60-second timeout and `max_retries=0`; the user retries
explicitly.

### The preamble digest

Every document made with `n` starts from one `preamble.tex`, so its packages and
macros are the conventions a snippet has to match. Sending that file with every
request would be wasteful and would crowd out the document, so it is summarised
once by the cheaper `TEXMAN_OPENAI_MINI_MODEL` and the summary is cached at
`$XDG_DATA_HOME/texman/preamble-digest.json`, beside the catalog.

`texman/preamble.py` owns the cache and knows nothing about OpenAI:
`ensure_digest` takes the function that produces a summary, so the caller owns
the request and tests stub it. The cache is keyed on the preamble's SHA-256, so
editing `preamble.tex` invalidates it and leaving it alone costs nothing. Writes
go through a temporary file and `os.replace`, so a crash never leaves half a
cache, and a cache that is absent, unreadable, malformed, or describes different
contents is a miss rather than an error.

`texman preamble` refreshes it on demand (`--force`, `--show`) and reports any
failure, because the user asked for it. An editing request seeds it silently
through `resolve_digest`, which swallows every failure: a missing preamble, an
unset mini model, or a failed summary leaves `:TexAI` working exactly as it did
before the digest existed. Nothing here needs a key when the cache is current,
because `digest_producer` builds the client inside the producer, not around it.

### Bounded context

Context is built only from the buffer the user is editing — no other indexed
file is read and `\input` is not followed; the shared preamble template is the
single named exception, and even that is read once per edit, not per request.

Every context line is rendered as `   765| text`. The numbers are what makes a
prompt like "look at the previous 30 lines" answerable: without them the model
cannot tell which line is which, and across an omitted stretch it cannot even
count. Overlapping ranges are merged, omitted stretches are labelled
(`[... lines 101-139 omitted ...]`), and the insertion point is marked in place.

Two shapes:

- **Windowed**, the default. Up to the first 100 lines supply packages and
  macros — 30 when a digest already names the shared ones — and up to
  `TEXMAN_WINDOW_LINES` (default 10) lines on each side of the insertion point
  supply local conventions. Capped at 24,000 characters.
- **Whole file**, from `:TexAI!` or `TEXMAN_FULL_FILE=1`. Every line goes out,
  numbered, so the prompt may refer to any of them. Capped at 120,000
  characters.

When a result exceeds its cap, the preamble is shed first and the lines nearest
the insertion point are kept longest. A whole-file request too large to send
falls back to a 100-line window rather than shrinking the buffer one line at a
time, which would re-render the whole document on every step.

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

Keymap mode has a third set of instructions: Lua only, `vim.keymap.set` with a
`desc`, LaTeX-specific mappings registered buffer-locally from a `FileType`
autocmd in the `texman_keymaps` group (named, never created or cleared, because
the loader owns that group), an unmapped key chosen when the request names
none, no snippet engine, and an opening comment that restates the request. The
request body carries the leader keys, the keys already mapped (left-hand sides
only, capped at 300), and the current mappings file, tail-truncated at 8,000
characters because the newest mappings are at the end and set the style.

Generation is the only network operation in the program, and it sends only the
prompt and, by mode, the bounded buffer context, the bounded log excerpt, or the
mappings state above. The catalog and descriptions stay local.

## Neovim integration

`nvim/texman.lua` registers `:TexAI <line> <prompt>` with
`nvim_create_user_command`. An uppercase initial is required for user commands,
and it leaves `/` alone so ordinary search is unaffected. If the name is already
taken, `setup` reports the conflict and returns `false`;
`require('texman').setup({ command = 'OtherName' })` picks another.

The command takes a bang. `:TexAI!` sets `full` on the request, sending the
whole buffer as numbered context so the prompt may refer to any part of it;
`TEXMAN_FULL_FILE=1` makes that the default. The progress notification says
which shape was used, so the two are never confused after the fact.

One request per buffer at a time, tracked in a table keyed by buffer handle and
cleared on every exit path, including failures.

The request captures the buffer handle and its *in-memory* lines, so unsaved
edits are part of the context. `vim.system({'texman','ai'}, …)` runs
asynchronously with the JSON on stdin and the environment inherited; there is
no shell command string, so neither the key nor the prompt is ever exposed to
shell quoting.

The insertion point is an extmark, placed by `track` on line `L` (or on the
last line when appending), in the `texman` namespace. Extmarks move with the
text, so the user can keep typing while the request runs: lines added or
removed above the target shift the mark, and the result still lands before the
line the user named. The first version compared `changedtick` instead and
discarded the result whenever the buffer had changed, which in practice meant
every request the user did not sit still for. Only a buffer that is closed or
no longer modifiable discards a result now; the `guarded` wrapper checks those
two before `apply` reads the mark's current row and makes its one
`nvim_buf_set_lines` call. Switching windows in the meantime is fine, because
insertion targets the captured handle rather than the current buffer.

The same extmark is the progress indicator. `track` decorates it with a
virtual line above the insertion point (below the last line when appending)
carrying a spinner frame, the seconds elapsed, and the prompt, redrawn by a
`vim.uv` timer every 120 ms through `vim.schedule_wrap`; `:TexAIFix` uses
end-of-line virtual text on the blamed line instead. A tracker's `stop`
closes the timer and deletes the mark, and `run` calls it on every exit path —
success, failure, empty output, launch error, and a busy refusal — so a
spinner can never outlive its request. After a successful edit, `flash`
highlights the new lines with a second extmark for 1.5 s. None of this touches
buffer text or `changedtick`; the indicator is decoration only.

Messages go through `notify`, which does two things `vim.notify` alone does
not. It cuts the text to one screen line, because a wrapped message ends in a
"Press ENTER" prompt that swallows the user's next keystroke. And it looks at
`nvim_get_mode()` first: while the user is in command-line mode, at a prompt
(`r`, `rm`, `r?`), or `blocking`, the message is queued and a 100 ms timer
delivers it once the mode is safe. An asynchronous echo during command-line
editing is what made Neovim repeat the half-typed command on a new line at
every following keystroke, the bug the user described as the command
"repeating itself a ton". The queue delivers everything, in order, exactly
once.

Every `:TexAI` request is recorded per buffer before validation, so a prompt
refused for a bad line number is recorded too. `:TexAIPrompt` (`M.recall`)
feeds `:TexAI[!] <line> <prompt>` to the command line with `nvim_feedkeys` and
no carriage return, so the user edits and resends deliberately; it falls back
to the last request made anywhere when the current buffer has none, and repeats
`!` only when it was typed, since a bang from `TEXMAN_FULL_FILE` still applies.

`:TexAIFix` reuses all of that plumbing and adds the log work. Its tracker sits
on the blamed line, and `apply` refuses when the text at the mark no longer
matches what the compiler saw, so typing elsewhere is fine but an edited blamed
line is never replaced blind. It locates the log
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

`:TexPreamble` runs `texman preamble` rather than `texman ai`, which is why the
runner takes an argv and an optional stdin; `send` is the thin wrapper that
supplies `{'texman','ai'}` and the encoded JSON. It has its own concurrency key,
so summarising and generating can overlap but two summaries cannot. Running it
is optional — the first `:TexAI` after an edit does the same work — but it moves
the cost out of the way of a generation and is where a misconfiguration is
reported, since `:TexAI` stays silent about a failed summary by design. It is
registered optionally, like `:TexAIFix`, so losing it never costs `:TexAI`.

`:TexAIMap <description>` reuses `send` with the mappings file, not a buffer,
as its concurrency key, so a mapping request and a document request can
overlap but two mapping requests cannot. The buffer re-checks live in a
`guarded` wrapper that only the two document commands use. Drafted mappings go
to `stdpath('config')/texman-keymaps.lua` (`setup({ keymaps_file = ... })`
moves it); `init.lua` is never edited, because a syntax error appended there
would break every future start of Neovim. When the helper's Lua arrives it is
compiled with `loadstring` but not run; a draft that does not compile is
refused with the parser's message. Otherwise the file is opened in a split, a
header is added if the file is new, and the draft is appended after a comment
naming the request, as one undoable change, with the cursor on it. Nothing is
written: `:w` accepts the draft and `u` discards it, so the user reads the
code before Neovim ever executes it. `setup` runs the file at startup and a
`BufWritePost` autocmd runs it again after each write, both through
`load_keymaps`, which clears the `texman_keymaps` augroup first so re-running
the file leaves no duplicate autocmds and wraps `dofile` in `pcall` so a
failing file is reported rather than raised. The autocmd matches on resolved
paths instead of a pattern, which avoids pattern-escaping the config path.

## Verification

- `python -m unittest discover -s tests -t .` — 269 checks. `test_index.py`
  builds a temporary fixture with nested files, hidden folders, duplicate
  filenames, spaces, an uppercase extension, a symlink loop, a broken link, and
  a `chmod 000` directory, and covers description persistence across restarts,
  rescans, cancelled scans, and deleted files. `test_documents.py` covers the
  template and document creation against a temporary directory. `test_ai.py`
  stubs the client, so no test makes a paid API call. `test_tui.py` drives the
  app headlessly with Textual's pilot, stubbing `suspend()` and
  `subprocess.run`, and covers counts and sequences (including that `;s` never
  stops a scan and that digits type into the filter), the preamble, new
  documents, and late messages arriving after teardown. `test_preamble.py`
  covers the digest cache — round trips, staleness after an edit, a corrupt
  cache treated as a miss, clipping, and atomic writes — with the summary
  function stubbed, so it makes no request at all. `test_cli.py` runs the real
  command as a subprocess, including `texman ai` and `texman preamble` failure
  paths that need no API key. Every check in `test_ai.py` runs with XDG pointed
  at a temporary directory, because a generation request now looks up a cached
  summary and would otherwise touch the developer's own cache.
- `nvim --headless -u NONE -l tests/test_nvim.lua` — 214 checks against a fake
  `texman` executable on `PATH`, covering insertion before the first and a
  middle line, appending, invalid lines and prompts, unsaved context,
  backslashes in prompts, one-step undo, switched buffers, typing above, below,
  at, and after the target while a request runs, closed buffers, duplicate
  requests, helper failure, empty output, a missing helper, the indicator's
  placement, content, and removal on success, failure, and refusal, the
  completion flash, prompt recall with and without a bang, messages held during
  command-line and blocking modes and truncated to the screen width,
  every log shape `:TexAIFix` understands and every refusal, and for
  `:TexAIMap` the draft-review-write flow, one-step undo, refusal of Lua that
  does not compile, and a broken mappings file at startup. It also covers the
  bang and `TEXMAN_FULL_FILE` reaching the request as `full`, and `:TexPreamble`
  running `texman preamble`, passing `--force` for a bang, and reporting
  failure, silence, and a second summary started while one runs.
- Automated tests only ever scan a temporary fixture, and they always stub the
  client. Scanning `/` and one live request per command are manual checks,
  recorded in the README.

## Deliberate omissions

No server, daemon, filesystem watcher, ORM, vector database, agent framework,
document parser, streaming protocol, or automatic LaTeX compiler. Manual refresh
and one API request per prompt are enough. Also deferred: Windows support and
installer packaging, automatic deletion of stale rows, rename tracking, bulk
actions, tags, favorites, and file operations other than creating a document
from the template. Ignoring directories is no longer among them: it is in the
UI, stored in `ignored_dirs`, and honoured by scans. `:TexAIMap` drafts one
mapping per request into one file and never edits `init.lua`.
