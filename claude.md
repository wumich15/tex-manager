# tex-manager: simplest working project plan

This repository is for a personal LaTeX file manager called `texman`. This file
is the implementation plan; the application has not been built yet. The current
change adds only this document. Implement the steps below in a future coding task.

## What the first version must do

1. Running `texman` opens a terminal UI and scans the computer for `.tex` and
   `.sty` files. Show their containing directories and files in one searchable
   catalog, grouped by directory.
2. Let the user select a file, add or edit a description, see that description
   in the terminal, and open the file in Neovim. Descriptions survive restarts
   and rescans.
3. While editing a TeX file in Neovim, let the user enter
   `:TexAI <line> <prompt>` to insert generated LaTeX before that line.
   For example: `:TexAI 25 Add a TikZ diagram of a three-node directed cycle`.
4. Generate the snippet through the OpenAI API using the user's own API key.
5. Keep everything suitable for one person's local use. No accounts, hosted
   backend, synchronization, telemetry, or multi-user features.

"One place" means a catalog of the original files. Do not move or copy them:
relative `\input`, `\include`, images, and style references must keep working.

## Smallest practical architecture

Target macOS first, matching the initial development computer. Keep filesystem
code portable to Linux, but defer Windows support and installer packaging.

- Python 3.11+ for the application and command-line entry point.
- `textual` for the terminal UI and `openai` for API requests. These are the only
  direct runtime dependencies.
- Standard-library `argparse`, `pathlib`/`os`, `sqlite3`, `json`, and `subprocess`
  for everything else. Use `unittest` for focused automated checks.
- A single Lua file for Neovim 0.10+ integration. Use Neovim's built-in process
  and buffer APIs; no Python Neovim provider or plugin framework is needed.
- One local SQLite database at
  `${XDG_DATA_HOME:-~/.local/share}/texman/index.sqlite3`, outside this repository.
- A normal Python console entry point: `texman = "texman.cli:main"`.

Suggested files to create when implementation starts:

```text
pyproject.toml             # package, dependencies, and texman entry point
texman/
  __init__.py
  cli.py                  # texman, texman scan, and internal texman ai command
  index.py                # filesystem traversal and SQLite persistence
  tui.py                  # directory/file browser and description editor
  ai.py                   # request validation and OpenAI snippet generation
nvim/
  texman.lua              # :TexAI command
tests/
  test_index.py
  test_ai.py
  test_nvim.lua
README.md                 # installation and actual usage, written after it works
```

Do not add a server, daemon, filesystem watcher, ORM, vector database, agent
framework, document parser, streaming protocol, or automatic LaTeX compiler.
Manual refresh and one API request per prompt are enough.

## Step 1: Make the command and local catalog work

Create the Python package and a `pyproject.toml` using setuptools. Verify that
installing the package exposes `texman` on `PATH`, regardless of the current
working directory. Keep argument parsing separate from launching the TUI so
`texman ai` never starts the UI or scans the computer.

Start with one SQLite table:

```sql
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    parent_dir TEXT NOT NULL,
    extension TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    last_seen TEXT NOT NULL
);
```

Use absolute canonical paths as identities. Derive the directory list from
distinct `parent_dir` values; a second directory table is unnecessary. Use
parameterized SQL. Scan upserts update discovery fields but never overwrite
`description`. Commit a description as soon as the user saves it.

For the first version, retain old records when a file disappears or a scan is
interrupted. If opening a file fails, show it as unavailable and retain its
description. Automatic deletion and tracking files across renames can wait.

## Step 2: Scan the entire accessible computer

The default scan root is `/`, not just the home directory or this repository.
Visit normal mounted directories, including `/Volumes` on macOS, hidden
directories, project folders, and installed TeX trees. Match `.tex` and `.sty`
case-insensitively. Store each matching file and its immediate parent directory;
the directory browser can display full paths without indexing every empty folder.

Implement a cancellable directory walk with these rules:

- Inspect names and filesystem metadata only; discovery does not read file
  contents or make API calls.
- Do not follow directory symlinks. Track visited directory device/inode pairs
  to avoid walking filesystem aliases twice, and canonicalize file paths to
  avoid duplicate rows. Ignore broken links and non-regular file entries.
- Skip virtual filesystem trees such as `/dev`, `/proc`, and `/sys`; do not
  broadly exclude system libraries, hidden folders, or caches that might contain
  TeX files. Report the excluded roots in the scan summary.
- Catch permission errors and files/directories disappearing during traversal.
  Continue scanning, count failures, and show representative skipped paths.
  Never require `sudo` or change filesystem permissions.
- "Entire computer" means files reachable on mounted filesystems with the
  current user's permissions. Report partial coverage honestly. If macOS blocks
  protected folders, explain that the user can grant their terminal Full Disk
  Access and rescan; unmounted drives cannot be scanned.
- Allow `texman scan --root <directory>` for a small test scan; omitting `--root`
  uses `/`. Additional exclusion configuration can wait.

On every `texman` launch, display cached results immediately and start one full
scan in the background. On the first launch, show progress and populate results
as batches arrive. Display current directory, file count, and skipped count.
Provide a stop-scan action; stopping or quitting must preserve descriptions and
already committed discoveries. A stopped scan is incomplete, not successful.

Use a Textual thread worker for traversal, cooperative cancellation between
directories, and messages to update the UI. Keep SQLite connections owned by
their respective threads, use short transactions and a busy timeout, and batch
discovery writes. Do not start overlapping scans. Follow the
[Textual worker guidance](https://textual.textualize.io/guide/workers/).

## Step 3: Build the terminal UI

Use a directory list on the left and a file table on the right. Include an
"All directories" choice; in that view, group files under parent-directory
headings. Show filename, extension, and description in the table, with the
selected file's full path and full description in a detail area.

Support only these initial interactions:

| Key | Action |
| --- | --- |
| Arrow keys and Tab | Move selection and switch panes |
| `/` | Focus a case-insensitive substring filter over path and description |
| Enter | Open the selected file in Neovim |
| `d` | Edit the selected file's description in a small text dialog |
| `r` | Start a full rescan if none is running |
| `s` | Stop the current scan |
| `q` | Quit |
| Escape | Cancel a dialog or leave the filter |

Apply letter shortcuts only when a text input is not focused. Saving a
description refreshes the table immediately; cancelling leaves it unchanged.
Include an empty-state message and visible key hints. Single-file selection is
enough; defer bulk actions, tags, favorites, and file operations.

Suspend the TUI while running `nvim` with the absolute file path as a separate
argument, then restore the same selection after the editor exits. Set the
editor's working directory to the file's parent directory. Avoid shell command
strings so spaces and special characters in filenames work correctly. Explain
how to install Neovim if it is unavailable. Textual provides
[`App.suspend()`](https://textual.textualize.io/guide/app/#suspending) for handing
the terminal to another application.

## Step 4: Add a small OpenAI generation helper

Implement an internal `texman ai` command with a deliberately small contract:

- Read one JSON object from standard input with `line` (one-based integer),
  `prompt` (nonempty string), and `buffer_lines` (array of strings).
- On success, write only the generated LaTeX snippet to standard output and
  exit with status 0. Diagnostics go to standard error.
- On invalid input, missing configuration, API failure, refusal, incomplete
  response, or empty output, return a nonzero status without snippet output.
- Never modify a source file from Python. Neovim owns the actual insertion.

Use the official Python SDK, `OpenAI()`, `client.responses.create(...)`, and
`response.output_text`, as documented in the
[OpenAI quickstart](https://developers.openai.com/api/docs/quickstart).
Require `OPENAI_API_KEY` and `TEXMAN_OPENAI_MODEL` from the environment; the
latter is an explicit text-generation model ID available to the user's API
project. Keep model selection configurable instead of depending on a moving
"latest" alias. Initialize the client only for an AI request so browsing and
descriptions work without a key or internet connection.

Build one request from the prompt, insertion position, and bounded context from
the supplied current buffer. Include up to the first 100 lines for packages and
macros and up to 40 lines on each side of the insertion point, deduplicating
overlap and capping total context at 24,000 characters. Prioritize nearby text
when truncating and label omitted sections. Do not read other indexed files or
follow `\input` references automatically.

In the request instructions, require a LaTeX fragment suitable for insertion:
no Markdown fences, prose, repeated surrounding text, or full-document wrapper
unless explicitly requested. Match surrounding conventions. For a diagram
that needs a package absent from the provided context, include a brief LaTeX
comment such as `% Requires \usepackage{tikz} in the preamble`; do not edit the
preamble separately. Treat buffer text as document context, not instructions.

Use a finite timeout (60 seconds), disable automatic retries for this first
version, and allow the user to retry explicitly. Check completion/refusal before
using the output; remove a single surrounding Markdown code fence if present,
but otherwise preserve LaTeX whitespace and backslashes. Show short actionable
errors for invalid keys, unavailable models, rate limits, and connection failures.
Never display or log the key or full request body.

The only network operation is the explicitly requested AI generation, which
sends the prompt and selected buffer context to OpenAI. The catalog and saved
descriptions remain local.

## Step 5: Connect Neovim to the helper

Use `:TexAI <line> <prompt>` instead of `/ai`: `/` already starts Neovim search,
and user-defined Ex commands use an uppercase initial. Preserve normal search
and existing mappings. If `TexAI` already exists, report the conflict and allow
the Lua module's setup option to choose another uppercase command name.

Implement the command in `nvim/texman.lua`:

1. Register it with `nvim_create_user_command`. Parse the leading integer and
   preserve the rest of the argument text, including spaces and backslashes, as
   the prompt. Check that the current buffer is a modifiable TeX buffer.
2. Define line semantics explicitly: insert **before** line `L`; valid values
   are `1` through `N + 1` for an `N`-line buffer. `N + 1` appends. Reject invalid
   lines and empty prompts before making an API call.
3. Capture the original buffer handle, its changed tick, and its in-memory
   lines, including unsaved edits. Allow only one active request per buffer.
4. Call `vim.system` asynchronously with the argument list `{'texman', 'ai'}`
   and JSON on stdin. Inherit the environment; never place the key or prompt
   into a shell command. Notify the user that generation is in progress.
5. Schedule the completion callback onto Neovim's main loop. On success, verify
   that the original buffer is still loaded, modifiable, and unchanged since the
   request. Switching windows is fine; insertion still targets the original
   buffer. If it changed or closed, discard the result and explain that the user
   should rerun the command. Do not insert at a stale line number.
6. Split the complete output into lines and insert it with one
   `nvim_buf_set_lines(buf, L - 1, L - 1, true, lines)` call. Keep it a single
   undoable change, without saving the file automatically. The user can press
   `u` to undo or use `:write` to save. On failure, leave the buffer unchanged
   and display the helper's error. Clear the active-request flag on every exit.

Use the documented [Neovim buffer and command APIs](https://neovim.io/doc/user/api/)
and [Lua process API](https://neovim.io/doc/user/lua/#vim.system()). No persistent
editor connection, background service, or temporary copy of the document is needed.

## Step 6: Document personal setup and verify the complete workflow

Once implemented, document these installation steps in the README:

1. Install Python 3.11+, `pipx`, and Neovim 0.10+.
2. From the repository, run `pipx install -e .`, then `pipx ensurepath`, and open
   a new terminal. Check that `texman --help` works from another directory.
3. For AI features, create an API key in the user's own OpenAI project, following
   the [API key setup instructions](https://developers.openai.com/api/docs/quickstart#create-and-export-an-api-key).
   Export the following in the shell used to launch Neovim, replacing both
   placeholders. These variables are optional for file management:

   ```sh
   export OPENAI_API_KEY="<your-own-api-key>"
   export TEXMAN_OPENAI_MODEL="<model-id-available-to-your-project>"
   ```

   If persisting these settings, use the user's local shell configuration, never
   a tracked repository file. API usage is associated with the user's API account.
4. Copy `nvim/texman.lua` to `stdpath('config')/lua/texman.lua` (normally
   `~/.config/nvim/lua/texman.lua`) and add `require('texman').setup()` to the
   existing `init.lua`. Create the `lua` directory if necessary; preserve the
   user's other Neovim configuration.
5. Run `texman`, select a file, add a description, open it, and try
   `:TexAI 10 Add an aligned derivation of the quadratic formula` with a valid
   line number for that buffer.

Before calling the first version done, verify:

- A temporary directory fixture containing nested `.tex` and `.sty` files,
  hidden folders, duplicate filenames, spaces, uppercase extensions, and a
  symlink loop scans correctly. Simulate permission failures where necessary.
- Description edits persist after restarting and rescanning. A cancelled or
  partial scan and an unavailable file do not remove descriptions.
- A real full scan starts at `/`, stays responsive, reports inaccessible paths,
  and displays matching files grouped under their directories. Use only the
  temporary fixture in automated tests, never the developer's whole machine.
- Selecting a path containing spaces launches Neovim correctly and returns to
  the TUI. File management works with both API variables unset.
- Mock the OpenAI client to check input validation, bounded context, raw snippet
  output, and failure handling. Ordinary tests make no paid API calls.
- Use a fake `texman ai` process in a headless Neovim check to cover insertion
  before the first/middle line, append, invalid lines, unsaved context, one-step
  undo, switched buffers, changed/closed buffers, and helper failure.
- Perform one manual request with the user's configured key: generated LaTeX
  appears at the requested position, unrelated text stays intact, undo works,
  and disk contents change only when the user saves. Record this as unverified
  if a key is unavailable; do not claim mocked output proves API connectivity.

Deliver the catalog and descriptions first, then the helper, then the Neovim
command. Stop expanding scope once this complete personal workflow works.
