# tex-manager: working notes for `texman`

`texman` is a personal LaTeX file manager: a terminal catalog of the `.tex` and
`.sty` files already on the machine, a description per file, Neovim as the
editor, a `:TexAI <line> <prompt>` command that inserts generated LaTeX, and a
`:TexAIMap <prompt>` command that drafts a Neovim key mapping.

**The plan in this file has been implemented.** Everything in
"What the first version does" works and is verified as described under
"Verifying changes". Read [ARCHITECTURE.md](ARCHITECTURE.md) before changing
anything non-trivial; it explains the design decisions and the invariants below.
[README.md](README.md) is the user-facing install and usage guide.

## What the first version does

1. `texman` opens a terminal UI and scans `~/Documents` and `~/Downloads` for
   `.tex` and `.sty` files, showing their containing directories and files in
   one searchable catalog, grouped by directory. `--root DIRECTORY`, repeatable,
   scans somewhere else instead.
2. The user can select a file, add or edit a description, see that description in
   the terminal, and open the file in Neovim. Descriptions survive restarts and
   rescans. `i` hides a directory and everything under it from the catalog, and
   shows it again; scans skip what is hidden.
3. While editing a TeX file in Neovim, `:TexAI <line> <prompt>` inserts generated
   LaTeX before that line, for example
   `:TexAI 25 Add a TikZ diagram of a three-node directed cycle`.
   `:TexAIFix` reads the compiler log, finds the line LaTeX blamed, and replaces
   it with a corrected version. This was added after the first version shipped,
   at the user's request; it is part of the intended scope now.
4. Snippets are generated through the OpenAI API using the user's own API key.
5. Everything is suitable for one person's local use. No accounts, hosted
   backend, synchronization, telemetry, or multi-user features.
6. The TUI takes Vim-style motions with counts: `5j`, `3k`, `gg`, `G`, `h`/`l`
   to switch panes, and `;s` / `;a` for the next / previous directory. Added at
   the user's request after the first version.
7. `p` creates and opens a `preamble.tex` template; `n` creates a new `.tex`
   document from it, in a directory the user types, defaulting to the
   directory under the cursor. Also added at the user's request.
8. `:TexAIMap <prompt>` asks the model for a Neovim key mapping (for example an
   insert-mode shortcut that types `\begin{enumerate} \item \end{enumerate}`
   and leaves the cursor after `\item`), appends the Lua to
   `~/.config/nvim/texman-keymaps.lua` in a split for review, and activates it
   when that file is written. `init.lua` is never edited. Also added at the
   user's request.
9. Context lines carry their real line numbers, so a prompt can refer to the
   document: `:TexAI 765 look at the previous 30 lines and make a diagram for
   this`. `:TexAI!` sends the whole buffer rather than a window;
   `TEXMAN_WINDOW_LINES` (default 10) sets how wide that window is, and
   `TEXMAN_FULL_FILE=1` makes the bang the default. `:TexPreamble` summarises
   the shared `preamble.tex` once with `TEXMAN_OPENAI_MINI_MODEL` and caches the
   summary, so later requests match its packages and macros without re-reading
   or re-sending the file; the first `:TexAI` after an edit does the same by
   itself. All added at the user's request.
10. While a request runs, an extmark marks the insertion point in the buffer
   with a spinner, the elapsed seconds, and the prompt; it follows the line
   through the user's edits, so typing while waiting no longer discards the
   result. Inserted lines flash briefly. `:TexAIPrompt` puts the last `:TexAI`
   command back on the command line to edit and resend. Messages are held
   while the user is typing a `:` command or sitting at a prompt, and cut to
   one screen line, because an asynchronous message echoed at those moments
   made Neovim repeat the half-typed command on new lines at every keystroke.
   All added at the user's request.

"One place" means a catalog of the original files. Do not move or copy them:
relative `\input`, `\include`, images, and style references must keep working.

## Layout

```text
pyproject.toml            # setuptools package; texman = "texman.cli:main"
texman/
  __init__.py             # version only
  cli.py                  # texman, texman scan/ai/preamble
  index.py                # filesystem traversal and SQLite persistence
  documents.py            # preamble.tex template and new documents from it
  preamble.py             # the cached summary of that template
  tui.py, tui.tcss        # directory/file browser, description editor, dialogs
  ai.py                   # request validation and OpenAI generation (4 modes)
nvim/texman.lua           # :TexAI, :TexAIFix, :TexAIMap, :TexAIPrompt, :TexPreamble
tests/
  test_index.py           # walk + catalog, against a temporary fixture
  test_documents.py       # template and document creation, in a temp dir
  test_preamble.py        # the digest cache, with the summary stubbed
  test_ai.py              # request contract, with a stubbed client
  test_tui.py             # headless Textual pilot, with nvim stubbed
  test_cli.py             # the installed command, as a real subprocess
  test_nvim.lua           # headless Neovim, with a fake texman on PATH
ARCHITECTURE.md, README.md
```

The catalog is one SQLite database at
`${XDG_DATA_HOME:-~/.local/share}/texman/index.sqlite3`, outside this
repository. `TEXMAN_DB` and `--db` point it elsewhere; tests always do. The
preamble digest is cached beside it, at `preamble-digest.json`; tests point
`XDG_DATA_HOME` at a temporary directory so they never touch the real one.

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
- **Ignoring hides, it never deletes.** An `ignored_dirs` row hides a directory
  and everything under it, by whole path segments (`/a/b` hides `/a/b/c`, not
  `/a/bc`). Rows and descriptions survive untouched and come back exactly as
  they were. Ignored directories stay visible in the directory pane, under a
  separator, or nobody could restore them; selecting one shows the tree it
  hides, because the ignore filter applies to the aggregate view only. Ignoring
  or unignoring a directory already covered by an ignored parent is refused and
  explained, never silently applied.
- **Identities are absolute canonical paths**, extensions are matched
  case-insensitively and stored lowercased, and all SQL is parameterized.
- **Discovery reads metadata only** — no file contents, no network. The scan
  roots default to `~/Documents` and `~/Downloads`; `--root` overrides them and
  may be repeated. One walk spans every root with a shared visited set, so a
  file reachable from two roots is catalogued once. A missing *default* root is
  dropped, a missing *named* root is a clean error.
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
- **Letter shortcuts never fire while a text input has focus.** That includes
  the Vim motions and count digits: `on_key` clears its state and returns while
  an `Input` or a modal dialog has focus.
- **A consumed motion key never reaches a binding.** Counts and `;`/`g`
  sequences live in `TexmanApp.on_key`, which Textual runs before the App's
  binding check; every consumed key gets `prevent_default()` and `stop()`, so
  the `s` of `;s` never stops a scan. A non-motion key drops the count and
  proceeds to its binding; an unknown sequence is reported and does nothing;
  Escape cancels a pending sequence. Plain arrow keys stay with the widgets.
- **Directory motions skip the disabled `── ignored ──` separator.**
- **`i` acts on the directory in context** — the highlighted directory when the
  left pane has focus, otherwise the selected table row's directory. `n` uses
  the same rule to prefill the directory, falling back to the first scan root.
- **Creating a document never overwrites anything.** The file is opened with
  mode `x`, an existing file is a clean error, and `p` never rewrites an
  existing `preamble.tex`. A typed directory may be created; a name may not
  contain a path separator. The new file is upserted into the catalog at once.
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
- **`texman ai` never modifies a document.** It reads one JSON object on stdin,
  writes only the snippet to stdout on success, and exits nonzero with a short
  stderr message on invalid input, missing configuration, API failure, refusal,
  incomplete response, or empty output. The one file it writes at all is its own
  preamble-digest cache under the data directory; insertion still belongs
  entirely to Neovim.
- **The client is constructed only for a generation request**, so browsing and
  descriptions work with no API key, no network, and no `openai` import. Both
  `OPENAI_API_KEY` and `TEXMAN_OPENAI_MODEL` come from the environment, plus
  `TEXMAN_OPENAI_MINI_MODEL` for the digest; every model is an explicit ID, not
  a moving alias, and the mini variable has no default and never silently
  becomes the expensive model. One request, a 60-second timeout, no automatic
  retries, explicit user retry. A current digest cache needs no key at all,
  because `digest_producer` builds the client inside the producer.
- **Context is bounded and numbered.** Every context line is rendered
  `   765| text`; the prompt may name lines or ranges, and the instructions say
  the prefixes are display only and must never be echoed. Windowed by default:
  up to the first 100 lines (30 when a digest supplies the shared ones), up to
  `TEXMAN_WINDOW_LINES` (default 10) each side of the insertion point, 24,000
  characters. `:TexAI!` or `TEXMAN_FULL_FILE=1` sends the whole buffer instead,
  capped at 120,000 characters. Overlap merged, omissions labelled, nearby text
  kept longest; a whole-file request too large to send falls back to a 100-line
  window rather than shrinking one line at a time. Never read other indexed
  files or follow `\input`. Buffer text is document context, not instructions.
- **The preamble digest is summarised once and cached, never re-sent.** The
  shared `preamble.tex` is the single named exception to "read nothing else",
  and it is read once per edit of that file, not once per request. The cache at
  `$XDG_DATA_HOME/texman/preamble-digest.json` is keyed on the file's SHA-256,
  written atomically, and any absent, malformed, or mismatched cache is a miss
  rather than an error. **A failed digest must never fail a generation**:
  `resolve_digest` swallows everything and the request goes out as it did before
  the digest existed. Only `texman preamble` / `:TexPreamble` reports the
  failure, because there the user asked for it.
- **Never display or log the key or the full request body.**
- **`:TexAIMap` never edits `init.lua` and never runs unreviewed code.** Drafts
  go to the mappings file `setup()` loads. The helper's Lua is compiled with
  `loadstring` and refused if it fails, then appended to the file in a split
  as one undoable change and left unsaved: `:w` accepts it, `u` discards it.
  `load_keymaps` runs the file only at startup and after a write, clearing the
  `texman_keymaps` augroup first and wrapping `dofile` in `pcall`, so a broken
  mapping is reported and can never stop Neovim from starting. The keymap
  request sends only the prompt, the mappings file, the leader keys, and the
  left-hand sides of existing mappings.
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
  handle and its in-memory lines, and places an extmark on line `L` (on line
  `N` when appending) that follows the line through the user's edits; the
  scheduled callback re-checks that the buffer is loaded and modifiable, reads
  the mark's current row, and makes a single `nvim_buf_set_lines` call there,
  so the insertion is one undo step and lands before the line the user named
  even if they typed above it meanwhile. A `changedtick` check would discard
  the result whenever the user kept typing, which is the bug it replaced. One
  active request per buffer, with the flag cleared on every exit path. Nothing
  is saved automatically.
- **The indicator is decoration, never text.** The spinner line, the fix
  marker, and the completion flash are extmarks in the `texman` namespace
  (`virt_lines`, eol `virt_text`, and a timed `hl_group` range). They change no
  buffer text and no `changedtick`, they are removed on every exit path of the
  request, including refusals and launch failures, and a request refused
  before it started leaves none behind. `:TexAIFix` still compares the tracked
  line's text with the text the compiler saw, and refuses if it differs.
- **Messages never interrupt typing.** `notify` cuts every message to one
  screen line, so it can never wrap into a hit-enter prompt that swallows the
  next key, and it queues messages while `nvim_get_mode()` reports command-line
  mode, a prompt (`r*`), or `blocking`, delivering them once the mode is safe.
  Echoing during command-line editing is what made Neovim repeat the half-typed
  command on new lines. The queue is the only place a message waits; nothing is
  dropped.
- **`:TexAIPrompt` recalls, it never resends.** The last request per buffer is
  recorded before any refusal, so a rejected prompt can be recalled too; the
  command is fed to the command line with `nvim_feedkeys` and no `<CR>`, so
  the user edits it and decides. Only an explicit `!` is repeated.
- The only network operation is the explicitly requested AI generation. The
  catalog and saved descriptions stay local.

## Verifying changes

```sh
python -m unittest discover -s tests -t .       # 269 checks
nvim --headless -u NONE -l tests/test_nvim.lua  # 214 checks
```

`python` here means the project's `.venv/bin/python`; the system `python3` on
the development machine lacks `openai` and fails three `test_ai.py` checks.

Automated tests use only a temporary fixture — never the developer's whole
machine — and always stub the OpenAI client, so they make no paid API calls.
They also redirect `XDG_DATA_HOME` and `XDG_CONFIG_HOME`, because a generation
request now looks up a cached preamble summary.
The fixture covers nested files, hidden folders, duplicate filenames, spaces,
an uppercase extension, a symlink loop, a broken link, and a permission failure.
Note that `Custom.Sty` and `custom.sty` are the same file on macOS, so
case-variant fixture names must live in different directories.

Manual checks, when touching the relevant area:

- A real scan stays responsive, reports inaccessible paths, and groups files
  under their directories. Last measured from `/`: 42 s, 569,833 directories,
  24,180 files, 557 skipped. The default roots are far smaller. With that
  catalog cached and a full scan running, the UI's median event-loop tick
  stayed at 0.10 s with a 1.5 s worst-case hiccup during a table rebuild.
- `texman --help` works from a directory other than the repository.
- A path containing spaces opens in Neovim and returns to the TUI.
- File management works with both API variables unset.
- One real `:TexAI` request with a configured key: the LaTeX appears at the
  requested position, unrelated text stays intact, `u` undoes it, and the file
  on disk changes only after `:write`. **Done** — verified against a live
  project with `TEXMAN_OPENAI_MODEL=gpt-5.6-luna`; see the README. Mocked output
  alone never proves API connectivity, so redo this check by hand if the request
  path changes.
- One real `:TexAIMap` request: the draft compiles, lands in the mappings file
  unsaved, and works after `:w`. **Done** for the enumerate example with the
  same model, through headless Neovim and a scratch mappings file; see the
  README.
- One real `:TexPreamble` with `TEXMAN_OPENAI_MINI_MODEL` set, then a real
  `:TexAI! <line> look at the previous 30 lines …` on a document of 800+ lines:
  the summary is cached, the generated LaTeX reflects the lines the prompt
  named, and a second `:TexAI` makes no further summary call. **Not yet done by
  hand** — the digest and whole-file paths are covered by stubbed tests only,
  and mocked output never proves API connectivity.

## Do not add

No server, daemon, filesystem watcher, ORM, vector database, agent framework,
document parser, streaming protocol, or automatic LaTeX compiler. Manual refresh
and one API request per prompt are enough. `:TexAIFix` *reads* a log the user's
own compiler already produced; it never runs the compiler itself.

Also deferred on purpose: bulk actions, tags, favorites, file operations other
than creating a document from the template, stale-row pruning, rename tracking,
Windows support, and installer packaging. Ignoring directories is now in scope,
at the user's request, but only as the manual per-directory toggle described
above: no patterns, no globs, no configuration file. `:TexAIFix` fixes one line
from one error; it does not iterate, recompile, or repair a whole document.
`:TexAIMap` drafts one mapping per request into one file; it does not edit
`init.lua`, manage plugins, or run code the user has not saved. The Vim
motions are the ones listed; there is no general keymap layer or rebinding.
The progress indicator is one extmark per request and a short flash; there is
no notification framework, floating window, or statusline component, and
`:TexAIPrompt` recalls one command rather than keeping a browsable history
(Neovim's own command history already does that).
The preamble digest summarises one shared template into one cache file: no
per-document digests, no summarising a document's own inline preamble, no
chained or server-stored conversation state, and no second cache of anything
else. Whole-file context is the bang and the two environment variables; there
is no per-request window argument and no automatic choice between the two.
The complete personal workflow works; do not expand scope further without
being asked.
